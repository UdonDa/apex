import math
import torch
import importlib
import amp_C
from apex.multi_tensor_apply import multi_tensor_applier

class DistributedFusedAdam(torch.optim.Optimizer):

    """Implements Adam algorithm. Currently GPU-only.  Requires Apex to be installed via
    ``python setup.py install --cuda_ext --cpp_ext``.

    It has been proposed in `Adam: A Method for Stochastic Optimization`_.

    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups.
        lr (float, optional): learning rate. (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square. (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability. (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_
            (default: False) NOT SUPPORTED in FusedAdam!
        eps_inside_sqrt (boolean, optional): in the 'update parameters' step,
            adds eps to the bias-corrected second moment estimate before
            evaluating square root instead of adding it to the square root of
            second moment estimate as in the original paper. (default: False)
        use_mt (boolean, optional): use multi tensor apply for lower launch
            latency. (default: False)
        overlap_reductions(boolean, optional): whether to overlap reductions
            with bprop (default: True)
        num_prestats (integer, optional): number of fp64 stats that will be
            reduced during first fp16 gradient reduction block. 

    .. _Adam\: A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """

    def __init__(self, params,
                 lr=1e-3, bias_correction = True,
                 betas=(0.9, 0.999), eps=1e-8, eps_inside_sqrt = False,
                 weight_decay=0., max_grad_norm=0., amsgrad=False, use_mt=False,
                 amp_scale_adjustment=1.0, overlap_reductions=True, full_pipeline=True,
                 compute_L2_grad_norm=False, distributed_weight_update=0,
                 dwu_group_size=0, dwu_num_blocks=4, dwu_num_rs_pg=1, dwu_num_ar_pg=4,
                 dwu_num_ag_pg=0, revert_method=1, flat_mt=False,
                 dwu_num_chunks=4, predivide=True, e5m2_allgather=False,
                 do_not_flatten_model=False):
        global fused_adam_cuda
        fused_adam_cuda = importlib.import_module("fused_adam_cuda")

        self._amp_scale_adjustment = amp_scale_adjustment

        if use_mt:
            raise RuntimeError('DistributedFusedAdam does not support use_mt.')
        if amsgrad:
            raise RuntimeError('DistributedFusedAdam does not support the AMSGrad variant.')

        defaults = dict(lr=lr, bias_correction=bias_correction,
                        betas=betas, eps=eps, weight_decay=weight_decay,
                        max_grad_norm=max_grad_norm)
        super(DistributedFusedAdam, self).__init__(params, defaults)
        self.eps_mode = 0 if  eps_inside_sqrt else 1

        self._overflow_buf = torch.cuda.IntTensor([0])

        assert (len(self.param_groups) == 1), "More than one parameter group is not supported."

        # Way to revert a step
        # 3 -> undo kernel + double buffer (debug, print norm of difference)
        # 2 -> double buffer fp32 parameters
        # 1 -> undo kernel
        self._revert_method = revert_method
        if self._revert_method > 1:
            print("revert_method -> double buffer fp32 parameters, will consume more memory")

        self._last_step = False
        self._overlap_reductions = overlap_reductions
        self._global_scale = None
        self._num_blocks = dwu_num_blocks
        self._num_chunks = dwu_num_chunks
        self._predivide = predivide
        self._e5m2_allgather = e5m2_allgather
        self._do_not_flatten_model = do_not_flatten_model
        self._full_pipeline = full_pipeline
        self._compute_L2_grad_norm = compute_L2_grad_norm
        self._L2_grad_norm = None
        self._group_size = torch.cuda.device_count() if dwu_group_size <= 0 else dwu_group_size
        self._world_size = torch.distributed.get_world_size()
        self._num_groups = self._world_size // self._group_size
        self._rank_in_group = torch.distributed.get_rank() % self._group_size

        p_offset = 0
        p_i = 0
        self._param_state = None
        self._model_params = []
        self._grads_info = []
        for group in self.param_groups:
            self._param_group = group
            for p in group['params']:
                torch.distributed.broadcast(p,0)
                if not p.requires_grad:
                    continue
                self._model_params.append(p)
                state = self.state[p]
                if len(state) == 0:
                    state['step'] = 0
                if self._param_state is None:
                    self._param_state = state
                p_grads_size = p.numel()
                def wrapper(param, param_i, param_grads_size, param_offset):
                    def allreduce_hook(grad):
                        self._do_overlapped_reduction(param_i, param_grads_size, param_offset, grad)
                    param.register_hook(allreduce_hook)
                self._grads_info.append({"param_grads_size":p_grads_size, "param_offset":p_offset})
                wrapper(p, p_i, p_grads_size, p_offset)
                p_offset += p_grads_size
                # enforce 128b alignment (64 * fp16)
                p_offset = ((p_offset + 63) // 64) * 64 
                p_i += 1
        self._grads_generated = [False]*len(self._grads_info)
        self._flat_mt = flat_mt
        self._grads = []
        if self._overlap_reductions:
            self._current_block = self._num_blocks

        self._net_total_param_size = p_offset
        self._total_param_size = p_offset
        dwu_min_page_size = 256 * self._num_blocks * self._num_chunks * self._group_size
        self._total_param_size = ((self._total_param_size + dwu_min_page_size - 1) // dwu_min_page_size) * dwu_min_page_size
        self._block_size = self._total_param_size // self._num_blocks
        self._chunk_size = self._block_size // self._num_chunks
        self._shard_size = self._chunk_size // self._group_size
        print("self._net_total_param_size=%d, self._total_param_size=%d, dwu_min_page_size=%d, self._block_size=%d, self._chunk_size=%d, self._shard_size=%d" % (self._net_total_param_size, self._total_param_size,dwu_min_page_size,self._block_size,self._chunk_size,self._shard_size))

        self._low_param_i = [0]*self._num_blocks
        for block_id in range(self._num_blocks-1,-1,-1):
            p_i = len(self._grads_info)-1
            while p_i > 0 and self._grads_info[p_i]["param_offset"] > block_id*self._block_size:
                p_i -= 1
            self._low_param_i[block_id] = p_i
        print(self._low_param_i)

        self._flat_grads = torch.zeros([self._total_param_size], dtype=torch.float16, device='cuda')
        self._new_params = torch.zeros([self._total_param_size], dtype=torch.uint8 if self._e5m2_allgather else torch.float16, device='cuda')
        self._mega_shard_size = self._num_blocks * self._num_chunks * self._shard_size
        self._fp32_p = torch.zeros([self._mega_shard_size], dtype=torch.float32, device='cuda')
        self._fp32_m = torch.zeros([self._mega_shard_size], dtype=torch.float32, device='cuda')
        self._fp32_v = torch.zeros([self._mega_shard_size], dtype=torch.float32, device='cuda')
        # FIXME: Rethink fp16 label since it's either uint8 or fp16
        self._fp16_p = torch.zeros([self._mega_shard_size], dtype=torch.uint8 if self._e5m2_allgather else torch.float16, device='cuda')
        self._fp16_g = torch.zeros([self._mega_shard_size], dtype=torch.float16, device='cuda')

        self._individual_flat_grads = []
        for p_i, grads_info in enumerate(self._grads_info):
            self._individual_flat_grads.append(self._flat_grads[grads_info["param_offset"]:grads_info["param_offset"]+grads_info["param_grads_size"]])

        def _flat_split(p):
            def __blockify(p):
                return [p[block_id*self._block_size:(block_id+1)*self._block_size] for block_id in range(self._num_blocks)]
            def __chunkify(p):
                return [p[chunk_id*self._chunk_size:(chunk_id+1)*self._chunk_size] for chunk_id in range(self._num_chunks)]
            def __shardify(p):
                return [p[shard_id*self._shard_size:(shard_id+1)*self._shard_size] for shard_id in range(self._group_size)]
            list_of_blocks = __blockify(self._flat_grads)
            list_of_list_of_chunks = [__chunkify(block) for block in list_of_blocks]
            list_of_list_of_list_of_shards = [[__shardify(chunk) for chunk in chunks] for chunks in list_of_list_of_chunks]
            return list_of_blocks, list_of_list_of_chunks, list_of_list_of_list_of_shards
        self._flat_grads_blocks, self._flat_grads_chunks, self._flat_grads_shards = _flat_split(self._flat_grads)
        def _full_packed_split(p):
            def __shardify(p):
                return [p[mega_shard*self._mega_shard_size:(mega_shard+1)*self._mega_shard_size] for mega_shard in range(self._group_size)]
            def __blockify(p):
                return [p[block_id*self._num_chunks*self._shard_size:(block_id+1)*self._num_chunks*self._shard_size] for block_id in range(self._num_blocks)]
            def __chunkify(p):
                return [p[chunk_id*self._shard_size:(chunk_id+1)*self._shard_size] for chunk_id in range(self._num_chunks)]
            list_of_mega_shards = __shardify(p)
            list_of_list_of_mega_blocks = [__blockify(mega_shard) for mega_shard in list_of_mega_shards]
            list_of_list_of_list_of_mega_chunks = [[__chunkify(mega_block) for mega_block in mega_blocks] for mega_blocks in list_of_list_of_mega_blocks]
            return list_of_mega_shards, list_of_list_of_mega_blocks, list_of_list_of_list_of_mega_chunks
        self._new_params_mega_shards, self._new_params_mega_blocks, self._new_params_mega_chunks = _full_packed_split(self._new_params)
        def _packed_split(p):
            def __packed_blockify(p):
                packed_block_size = self._num_chunks*self._shard_size
                return [p[block_id*packed_block_size:(block_id+1)*packed_block_size] for block_id in range(self._num_blocks)]
            def __packed_chunkify(p):
                # in the packed format, each chunk contains one shard, so packed_chunk_size == self._shard_size
                return [p[chunk_id*self._shard_size:(chunk_id+1)*self._shard_size] for chunk_id in range(self._num_chunks)]
            list_of_blocks = __packed_blockify(p)
            list_of_list_of_chunks = [__packed_chunkify(block) for block in list_of_blocks]
            return list_of_blocks, list_of_list_of_chunks
        self._fp32_p_blocks, self._fp32_p_chunks = _packed_split(self._fp32_p)
        self._fp32_m_blocks, self._fp32_m_chunks = _packed_split(self._fp32_m)
        self._fp32_v_blocks, self._fp32_v_chunks = _packed_split(self._fp32_v)
        self._fp16_p_blocks, self._fp16_p_chunks = _packed_split(self._fp16_p)
        self._fp16_g_blocks, self._fp16_g_chunks = _packed_split(self._fp16_g)

        # This paragraph does two things:
        # 1) Copy model parameters into master buffer
        # 2) Create tensor lists for unpacking new parameter tensor after all-gather
        self._packed_flat_to_model_params = []
        for shard_id in range(self._group_size):
            for block_id in range(self._num_blocks):
                for chunk_id in range(self._num_chunks):
                    flat_shard_start = (((block_id * self._num_chunks + chunk_id) * self._group_size) + shard_id) * self._shard_size
                    flat_shard_end = flat_shard_start + self._shard_size
                    for p, grads_info in zip(self._model_params, self._grads_info):
                        flat_grad_start = grads_info["param_offset"]
                        flat_grad_end = flat_grad_start + grads_info["param_grads_size"]
                        clipped_start = (lambda a,b: a if a > b else b)(flat_grad_start, flat_shard_start)
                        clipped_end = (lambda a,b: a if a < b else b)(flat_grad_end, flat_shard_end)
                        if clipped_start < clipped_end:
                            grad_offset = clipped_start - flat_grad_start
                            grad_length = clipped_end - clipped_start
                            shard_offset = clipped_start - flat_shard_start
                            model_param_fragment = p.view(-1)[grad_offset:grad_offset+grad_length]
                            new_param_packed_fragment = self._new_params_mega_chunks[shard_id][block_id][chunk_id][shard_offset:shard_offset+grad_length]
                            self._packed_flat_to_model_params.append( (new_param_packed_fragment, model_param_fragment) )
                            if shard_id == self._rank_in_group:
                                # copy model parameters into master buffer
                                master_param_fragment = self._fp32_p_chunks[block_id][chunk_id][shard_offset:shard_offset+grad_length]
                                print("model_param_fragment.size()=%s, new_param_packed_fragment.size()=%s, master_param_fragment.size()=%s" % (str(model_param_fragment.size()), str(new_param_packed_fragment.size()), str(master_param_fragment.size())))
                                master_param_fragment.copy_(model_param_fragment)

        p_in, p_out = zip(*self._packed_flat_to_model_params)
        self._packed_flat_to_model_params = [p_in, p_out]

        self._distributed_weight_update = distributed_weight_update # Is this still needed?
        self._num_rs_pg = dwu_num_rs_pg
        self._num_ar_pg = dwu_num_ar_pg
        self._num_ag_pg = dwu_num_ag_pg
        if self._num_groups > 1:
            self._ar_pg = []
            for dev_i in range(self._group_size):
                ranks = [dev_i+j*self._group_size for j in range(self._num_groups)]
                for i in range(self._num_ar_pg):
                    grp = torch.distributed.new_group(ranks=ranks)
                    if torch.distributed.get_rank() in ranks:
                        self._ar_pg.append(grp)
            self._ar_st = [torch.cuda.Stream() for _ in range(self._num_ar_pg)]
            for ar_pg in self._ar_pg:
                torch.distributed.all_reduce(self._overflow_buf,group=ar_pg)
        rs_ranks = []
        for group_i in range(self._num_groups):
            rs_ranks.append([group_i*self._group_size+j for j in range(self._group_size)])
        self._rs_pg = []
        for group_i in range(self._num_groups):
            ranks = rs_ranks[group_i]
            for i in range(self._num_rs_pg):
                grp = torch.distributed.new_group(ranks=ranks)
                if torch.distributed.get_rank() in ranks:
                    self._rs_pg.append(grp)
            if self._compute_L2_grad_norm and torch.distributed.get_rank() in ranks:
                self._l2_grad_norm_pg = torch.distributed.new_group(ranks=ranks)
                torch.distributed.all_reduce(self._overflow_buf,group=self._l2_grad_norm_pg)
        self._rs_st = [torch.cuda.Stream() for _ in range(self._num_rs_pg)]
        for rs_pg in self._rs_pg:
            torch.distributed.all_reduce(self._overflow_buf,group=rs_pg)
        if self._num_ag_pg == 0:
            self._ag_pg = self._rs_pg
            self._ag_st = self._rs_st
            self._num_ag_pg = self._num_rs_pg
        else:
            self._ag_pg = []
            for group_i in range(self._num_groups):
                ranks = rs_ranks[group_i]
                for i in range(self._num_ag_pg):
                    grp = torch.distributed.new_group(ranks=ranks)
                    if torch.distributed.get_rank() in ranks:
                        self._ag_pg.append(grp)
            self._ag_st = [torch.cuda.Stream() for _ in range(self._num_ag_pg)]
            for ag_pg in self._ag_pg:
                torch.distributed.all_reduce(self._overflow_buf,group=ag_pg)
        self._l2_grad_norm_st = torch.cuda.Stream() if self._compute_L2_grad_norm else None
        self._completion_st = torch.cuda.Stream()

        self._reductions_works = [None]*self._num_blocks
        self._allgather_works = [None]*self._num_blocks

        import inspect
        assert ('no_copy' in inspect.getfullargspec(torch.distributed.reduce_scatter).args), "This version of c10d does not support no_copy option"


    def set_last_step(self, last_step):
        self._last_step = last_step
        
    def _get_flush_block(self):
        flush_block = []
        if self._grads_generated[self._low_param_i[self._current_block-1]]:
            num_grads = len(self._grads_generated)
            contiguous_idx = num_grads
            while contiguous_idx > 0 and self._grads_generated[contiguous_idx-1]:
                contiguous_idx -= 1

            if contiguous_idx < num_grads and self._grads_info[contiguous_idx]["param_offset"] <= (self._current_block-1)*self._block_size:
                self._current_block -= 1
                start = self._current_block * self._block_size
                end = (self._current_block+1) * self._block_size
                flush_block = [start, end]

            if self._current_block == 0:
                # reset
                self._grads_generated = [False]*len(self._grads_info)

        return flush_block

    def _pipeline_block_reductions(self, block_id):
        self._flatten_grad_mt(1.0/self._world_size if self._predivide else 1.0)

        # Reduction within each node
        # Changes gradient format from [block * chunk * shard] to [shard * block * chunk]
        # The output format is the same as the fp32 master parameters
        works = [None]*self._num_chunks
        for chunk_id in range(self._num_chunks):
            glob_chunk_id = block_id * self._num_chunks + chunk_id
            rs_stream = self._rs_st[glob_chunk_id%self._num_rs_pg]
            rs_stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(rs_stream):
                works[chunk_id] = torch.distributed.reduce_scatter(self._fp16_g_chunks[block_id][chunk_id],self._flat_grads_shards[block_id][chunk_id],group=self._rs_pg[glob_chunk_id%self._num_rs_pg],async_op=True,no_copy=True)

        # Reduction across nodes for each rank
        if self._num_groups > 1:
            for chunk_id in range(self._num_chunks):
                glob_chunk_id = block_id * self._num_chunks + chunk_id
                ar_stream = self._ar_st[glob_chunk_id%self._num_ar_pg]
                with torch.cuda.stream(ar_stream):
                    works[chunk_id].wait()
                    works[chunk_id] = torch.distributed.all_reduce(self._fp16_g_chunks[block_id][chunk_id],group=self._ar_pg[glob_chunk_id%self._num_ar_pg],async_op=True)
        self._reductions_works[block_id] = works

        # Optionally compute L2 grad norm
        if self._compute_L2_grad_norm and block_id == 0:
            with torch.cuda.stream(self._l2_grad_norm_st):
                for block_id in range(self._num_blocks):
                    for chunk_id in range(self._num_chunks):
                        self._reductions_works[block_id][chunk_id].wait()
                # Since the packed format is contiguous after reductions, only one norm is needed
                l2_grad_norm_sq = torch.empty([1], device='cuda')
                l2_grad_norm_sq = self._fp16_g.norm(dtype=torch.float32, p=2)**2
                torch.distributed.all_reduce(l2_grad_norm_sq, group=self._l2_grad_norm_pg)
                self._L2_grad_norm = l2_grad_norm_sq.sqrt()

    def __launch_step_kernel(self, p, p_copy, m, v, g):
        combined_scale = self._global_scale
        if self._param_group['max_grad_norm'] > 0 and math.isfinite(self.L2_grad_norm):
            combined_scale = self._param_group['max_grad_norm'] / (self.L2_grad_norm / self._global_scale + 1e-6)
            combined_scale = self._global_scale / min(1, combined_scale)
        bias_correction = 1 if self._param_group['bias_correction'] else 0
        beta1, beta2 = self._param_group['betas']
        fused_adam_cuda.adam(
                p, p_copy, m, v, g,
                self._param_group['lr'],
                beta1,
                beta2,
                self._param_group['eps'],
                combined_scale,
                self._param_state['step']+1,
                self.eps_mode,
                bias_correction,
                self._param_group['weight_decay'])

    def _pipeline_block_step(self, block_id):
        # Call step kernel once per block
        ag_stream = self._ag_st[block_id%self._num_ag_pg]
        with torch.cuda.stream(ag_stream):
            for chunk_id in range(self._num_chunks):
                self._reductions_works[block_id][chunk_id].wait()
            self.__launch_step_kernel(
                self._fp32_p_blocks[block_id],
                self._fp16_p_blocks[block_id],
                self._fp32_m_blocks[block_id],
                self._fp32_v_blocks[block_id],
                self._fp16_g_blocks[block_id])
        # Call all-gather once per step.
        # FIXME: Determine which is faster, one all-gather per block or a single all-gather at end
        if block_id == 0:
            for other_ag_stream in self._ag_st:
                self._completion_st.wait_stream(other_ag_stream)
            with torch.cuda.stream(self._completion_st):
                torch.distributed.all_gather(self._new_params_mega_shards, self._fp16_p, group=self._ag_pg[0], no_copy=True)

    def _pipeline_step(self):
        # Call step kernel once per step
        # Call all-gather once per step
        with torch.cuda.stream(self._completion_st):
            for block_id in range(self._num_blocks):
                for chunk_id in range(self._num_chunks):
                    self._reductions_works[block_id][chunk_id].wait()
            self.__launch_step_kernel(
                self._fp32_p,
                self._fp16_p,
                self._fp32_m,
                self._fp32_v,
                self._fp16_g)
            torch.distributed.all_gather(self._new_params_mega_shards, self._fp16_p, group=self._ag_pg[0], no_copy=True)

    def _flatten_grad_mt(self, scale):
        if self._flat_mt and len(self._grads) > 0:
            self._overflow_buf.zero_()
            multi_tensor_applier(
                    amp_C.multi_tensor_scale,
                    self._overflow_buf,
                    list(zip(*self._grads)),
                    scale)
            self._grads = []

    def _do_overlapped_reduction(self, param_i, param_grads_size, param_offset, grad):
        # handle overlapped reductions
        if self._flat_mt:
            self._grads.append( (grad.view(-1), self._individual_flat_grads[param_i]) )
        else:
            torch.div(grad.view(-1), self._world_size if self._predivide else 1.0, out=self._flat_grads[param_offset:param_offset+param_grads_size])
        self._grads_generated[param_i]=True
        if not self._last_step:
            if self._overlap_reductions:
                flush_block = self._get_flush_block()
                while flush_block:
                    block_id = flush_block[0] // self._block_size
                    self._pipeline_block_reductions(block_id)
                    if self._full_pipeline:
                        self._pipeline_block_step(block_id)
                    flush_block = self._get_flush_block()

    def set_global_scale(self, global_scale):
        """Set global scale.
        """
        self._global_scale = global_scale

    @property
    def global_scale(self):
        return self._global_scale

    @property
    def has_overflow(self):
        """Check if overflows were detected by any call to step(...) method.
        Clears the overflow flag.
        """
        has_overflow = self._overflow_buf.item()
        self._overflow_buf.zero_()
        return has_overflow

    @property
    def peek_overflow(self):
        """Check if overflows were detected by any call to step(...) method.
        Does not clear overflow flag.
        """
        return self._overflow_buf.item()

    def strided_check_finite(self, output_params, stride=1, start=-1, end=-1, clear=True):
        """Strided check for overflow.
        You can get status by calling has_overflow.
        """
        if start >= 0 and start < end:
            out_p = output_params[start:end]
        else:
            out_p = output_params
        fused_adam_cuda.strided_check_finite(self._overflow_buf,
                out_p,
                stride,
                1 if clear else 0)

    @property
    def L2_grad_norm(self):
        if self._compute_L2_grad_norm:
            torch.cuda.current_stream().wait_stream(self._l2_grad_norm_st)
            return self._L2_grad_norm
        else:
            return None

    def complete_reductions(self):
        """Complete reductions if full pipeline is not selected or overlap is not allowed.
        """

        if self._last_step:
            # zero out gradients that have not been completed yet
            for param_i, grad_generated in enumerate(self._grads_generated):
                if not grad_generated:
                    grad_info = self._grads_info[param_i]
                    param_offset = grad_info["param_offset"]
                    param_size = grad_info["param_grads_size"]
                    self._flat_grads[param_offset:param_offset+param_size].zero_()
                    self._grads_generated[param_i] = True

        if self._last_step or not self._overlap_reductions:
            # nothing done so far, run full pipeline after reductions
            for block_id in range(self._num_blocks-1,-1,-1):
                self._pipeline_block_reductions(block_id)

        if self._compute_L2_grad_norm:
            torch.cuda.current_stream().wait_stream(self._l2_grad_norm_st)

        self._current_block = self._num_blocks
        self._grads_generated = [False]*len(self._grads_info)

    def revert_step(self):
        """Revert effect of previously calling partial_step.
        """
        # Call undo kernel once per step
        combined_scale = self._global_scale
        if self._param_group['max_grad_norm'] > 0 and math.isfinite(self.L2_grad_norm):
            combined_scale = self._param_group['max_grad_norm'] / (self.L2_grad_norm / self._global_scale + 1e-6)
            combined_scale = self._global_scale / min(1, combined_scale)
        bias_correction = 1 if self._param_group['bias_correction'] else 0
        beta1, beta2 = self._param_group['betas']
        fused_adam_cuda.maybe_adam_undo(
                    torch.empty([0]),
                    self._fp32_p,
                    self._fp32_m,
                    self._fp32_v,
                    self._fp16_g,
                    self._param_group['lr'],
                    beta1,
                    beta2,
                    self._param_group['eps'],
                    combined_scale,
                    self._param_state['step']+1,
                    self.eps_mode,
                    bias_correction,
                    self._param_group['weight_decay'])

    def step(self, closure=None, skip_overflow_check=False):
        loss = None
        if closure is not None:
            loss = closure()

        if self._last_step or not self._overlap_reductions or not self._full_pipeline:
            self._pipeline_step()

        with torch.cuda.stream(self._completion_st):
            # Check for overflow
            # Store state for loss scaler calculation
            if skip_overflow_check:
                has_overflow = False
            else:
                self.strided_check_finite(self._new_params, stride=self._shard_size, start=0, end=self._net_total_param_size)
                has_overflow = self.peek_overflow
            if has_overflow:
                print("Reverting step")
                self.revert_step()
            else:
                # Copy self._new_params to model params
                for p in self._model_params: self.state[p]['step'] += 1
                multi_tensor_applier(
                        fused_adam_cuda.maybe_cast_mt,
                        self._overflow_buf,
                        self._packed_flat_to_model_params)

        torch.cuda.current_stream().wait_stream(self._completion_st)

        self._reductions_works = [None]*self._num_blocks
        self._allgather_works = [None]*self._num_blocks

        return loss


