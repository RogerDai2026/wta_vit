import torch
import copy
import numpy as np
import torch.nn.functional as F
from torch import device, topk
import torch.distributed as dist
import ipdb

class Scheme(object):

    num_samples = 1
    batch = None

    _all_grad_A_shape = []
    _all_grad_B_shape = []
    _grad_A_index_offset = 0
    _grad_B_index_offset = 0 
    batch_whole_grad_A = None
    batch_whole_grad_B = None

    _all_grad_shape = []
    _grad_index_offset = 0
    batch_whole_grad = None
    _whole_grad_buffer_cat = None

    all_grad_A_size = 0
    all_grad_B_size = 0
    all_grad_size = 0
    # iter0 = 0
    scheme_open = False
    
    world_size = None
    rank = None
    group = None

    def __init__(self, sample_ratio, deter_ratio, deter_adaptive, minimal_k, sample_replacement, mix_replacement, batch_dim_use_same_indices):
        self.epoch = 0
        self.grad_updated = False
        self.inited = False
        self.sample_ratio = sample_ratio  
        self.deter_ratio = deter_ratio
        self.deter_adaptive = deter_adaptive
        self.minimal_k = minimal_k
        self.sample_replacement = sample_replacement
        self.mix_replacement = mix_replacement
        self.batch_dim_use_same_indices = batch_dim_use_same_indices
        self.q_bit = 2
        Scheme.scheme_open = True
        
        # print(id(Scheme.batch_whole_grad_A))
        # print(id(Scheme.batch_whole_grad_B))
        # print(id(Scheme.batch_whole_grad))  
        # print(id(Scheme._grad_A_index_offset))
        # print(id(Scheme._grad_B_index_offset))
        # print(id(Scheme._grad_index_offset))

    def scheme_init(self, mat_shape, device):

        return NotImplementedError

    def get_scale(self):

        return NotImplementedError

    def set_scale(self, grad):

        return NotImplementedError

    @classmethod
    def _init_buffer(cls):
        dtype = torch.bfloat16
        device = 'cpu'
        # import ipdb; ipdb.set_trace()

        cls.all_grad_A_size = np.sum([np.prod(shape) for shape in cls._all_grad_A_shape]).astype(np.int32)
        cls.all_grad_B_size = np.sum([np.prod(shape) for shape in cls._all_grad_B_shape]).astype(np.int32)
        #print(cls._all_grad_shape)
        cls.all_grad_size = np.sum([np.prod(shape) for shape in cls._all_grad_shape]).astype(np.int32)
        #print(cls.all_grad_A_size)
        #print(cls.all_grad_size)
        if cls.world_size is not None and cls.world_size >= 2: 
            if cls.rank == 0:
                buf_size = Scheme.num_samples * (cls.all_grad_A_size + cls.all_grad_B_size + cls.all_grad_size) * 2 / (1024 ** 3)
                print(Scheme.num_samples, cls.all_grad_A_size, cls.all_grad_B_size, cls.all_grad_size)
                print("Memory of Gradient Buffer:", buf_size, "GB")
                cls._whole_grad_buffer_cat = torch.zeros((Scheme.num_samples, cls.all_grad_A_size + cls.all_grad_B_size + cls.all_grad_size), dtype=dtype, device=device)
                
            else:
                pass
        else:
            buf_size = Scheme.num_samples * (cls.all_grad_A_size + cls.all_grad_B_size + cls.all_grad_size) * 2 / (1024 ** 3)
            #print("Memory of Gradient Buffer:", buf_size, "GB")
            cls._whole_grad_buffer_cat = torch.zeros((Scheme.num_samples, cls.all_grad_A_size + cls.all_grad_B_size + cls.all_grad_size), dtype=dtype, device=device)
        
    @classmethod
    def step(cls):
        if cls.world_size is not None and cls.world_size >= 2:
            Scheme.step_multi_process()
        else:
            Scheme.step_single_process()
            
    @classmethod    
    def step_single_process(cls):
        cls._whole_grad_buffer_cat[cls.batch] = torch.cat([cls.batch_whole_grad_A,
                                                            cls.batch_whole_grad_B,
                                                            cls.batch_whole_grad], dim=1).cpu()
    
    # @classmethod
    # def step_multi_process(cls):
        
    #     local_gradnorm = torch.cat([cls.batch_whole_grad_A,
    #                                 cls.batch_whole_grad_B,
    #                                 cls.batch_whole_grad], dim=1)
    #     index_tensor = torch.tensor(Scheme.batch, dtype=torch.int, device=local_gradnorm.device)
    #     all_index_list = [torch.empty_like(index_tensor) for i in range(cls.world_size)]
    #     gradnorm_tensor_list = [torch.empty_like(local_gradnorm) for i in range(cls.world_size)]
    #     dist.all_gather(gradnorm_tensor_list, local_gradnorm, group=cls.group)
    #     dist.all_gather(all_index_list, index_tensor, group=cls.group)
    #     all_batch = torch.cat(all_index_list).long()
    #     all_gradnorm = torch.cat(gradnorm_tensor_list).cpu()
    #     cls._whole_grad_buffer_cat[all_batch] = all_gradnorm
    #     # print("GPU", cls.rank, "Peak MEM:", peak_mem1, peak_mem2, peak_mem3, peak_mem4)
    #     del local_gradnorm, index_tensor, all_index_list, gradnorm_tensor_list, all_batch, all_gradnorm
    
    @classmethod
    def step_multi_process(cls):
        
        local_gradnorm = torch.cat([cls.batch_whole_grad_A,
                                    cls.batch_whole_grad_B,
                                    cls.batch_whole_grad], dim=1)
        if cls.rank == 0:
            # create an empty list we will use to hold the gathered values
            gradnorm_list = [torch.empty_like(local_gradnorm) for i in range(cls.world_size)]
            dist.gather(local_gradnorm, gather_list=gradnorm_list, dst=0, group=cls.group)
        else:
            dist.gather(local_gradnorm, gather_list=[], dst=0, group=cls.group)
        # only rank 0 will have the tensors from the other processed
        if cls.rank == 0:
            assert cls._index_tensor_list is not None
            for ind, t in zip(cls._index_tensor_list, gradnorm_list):
                cls._whole_grad_buffer_cat[ind] = t.cpu()
        

    # @classmethod
    # def fetch_data(cls):

    #     _whole_grad_buffer_cat_cuda = cls._whole_grad_buffer_cat[cls.batch].cuda()
    #     # grad_A_size = np.sum([np.prod(shape) for shape in cls._all_grad_A_shape])
    #     # grad_B_size = np.sum([np.prod(shape) for shape in cls._all_grad_B_shape])
    #     # grad_size = np.sum([np.prod(shape) for shape in cls._all_grad_shape])
    #     #
    #     cls.batch_whole_grad_A = _whole_grad_buffer_cat_cuda[:, :cls.all_grad_A_size]
    #     cls.batch_whole_grad_B = _whole_grad_buffer_cat_cuda[:, cls.all_grad_A_size:cls.all_grad_A_size + cls.all_grad_B_size]
    #     cls.batch_whole_grad = _whole_grad_buffer_cat_cuda[:, cls.all_grad_A_size + cls.all_grad_B_size:]
    #     # print(cls.batch_whole_grad)
    
    @classmethod
    def fetch_data(cls):
        
        if cls.world_size is not None and cls.world_size >= 2:
            Scheme.fetch_data_multi_process()
        else:
            Scheme.fetch_data_single_process()

    @classmethod
    def fetch_data_single_process(cls):
        # print(f"_whole_grad_buffer_cat shape before transfer: {cls._whole_grad_buffer_cat.shape}")
        # print(cls.batch)
        # print(f"_whole_grad_buffer_cat_cuda shape: {cls._whole_grad_buffer_cat[cls.batch:].cuda().shape}")
        _whole_grad_buffer_cat_cuda = cls._whole_grad_buffer_cat[cls.batch].cuda()
        # print(cls.all_grad_A_size)
        # print(cls.all_grad_B_size)
        cls.batch_whole_grad_A = _whole_grad_buffer_cat_cuda[:, :cls.all_grad_A_size]
        cls.batch_whole_grad_B = _whole_grad_buffer_cat_cuda[:, cls.all_grad_A_size:cls.all_grad_A_size + cls.all_grad_B_size]
        cls.batch_whole_grad = _whole_grad_buffer_cat_cuda[:, cls.all_grad_A_size + cls.all_grad_B_size:]
        # print(cls.batch_whole_grad)

    @classmethod
    def fetch_data_multi_process(cls):
        index_tensor = torch.tensor(Scheme.batch, dtype=torch.long, device=f'cuda:{cls.rank}')
        # only rank 0 will have the index tensors from the other GPUs
        if cls.rank == 0:
            # create an empty list we will use to hold the gathered values
            index_tensor_list = [torch.empty_like(index_tensor) for _ in range(cls.world_size)]
            dist.gather(index_tensor, gather_list=index_tensor_list, dst=0, group=cls.group)
            cls._index_tensor_list = [index_tensor.cpu() for index_tensor in index_tensor_list]
        else:
            tensor_list = None
            dist.gather(index_tensor, gather_list=[], dst=0, group=cls.group)

        # send the grad norm to other device
        gradnorm_size = (len(Scheme.batch), cls.all_grad_A_size + cls.all_grad_B_size + cls.all_grad_size)
        tensor = torch.empty(gradnorm_size, dtype=torch.bfloat16, device=f'cuda:{cls.rank}')
        if cls.rank == 0:
            tensor_list = [cls._whole_grad_buffer_cat[index_tensor_list[i].cpu()].cuda() for i in range(cls.world_size)]
            dist.scatter(tensor, scatter_list=tensor_list, src=0, group=cls.group)
        else:
            dist.scatter(tensor, scatter_list=[], src=0, group=cls.group)

        _whole_grad_buffer_cat_cuda = tensor
        cls.batch_whole_grad_A = _whole_grad_buffer_cat_cuda[:, :cls.all_grad_A_size]
        cls.batch_whole_grad_B = _whole_grad_buffer_cat_cuda[:, cls.all_grad_A_size:cls.all_grad_A_size + cls.all_grad_B_size]
        cls.batch_whole_grad = _whole_grad_buffer_cat_cuda[:, cls.all_grad_A_size + cls.all_grad_B_size:]
        # print(cls.batch_whole_grad)



class Scheme_3D_new(Scheme):

    def scheme_init(self, mat_shape):
        assert len(mat_shape) == 3
        _, c, _ = mat_shape
        self.grad_size = c
        self.grad_shape = (c,)
        Scheme._all_grad_shape.append(self.grad_shape)
        self.grad_index_offset = copy.deepcopy(Scheme._grad_index_offset)
        Scheme._grad_index_offset += self.grad_size
        self.inited = True

    def get_scale(self):
        if self._whole_grad_buffer_cat is None:  # self.grad_updated: #
            return None
        else:
            bs = Scheme.batch_whole_grad.size(0)
            grad_ = Scheme.batch_whole_grad[:, self.grad_index_offset:self.grad_index_offset + self.grad_size]
            self.grad_updated = False
            #print("Get Scale:", id(Scheme._whole_grad_buffer_cat), "=====", id(Scheme.batch_whole_grad))
            return grad_.view(bs, self.grad_size).contiguous()

    def set_scale(self, grad):
        grad_ = grad.view(-1, self.grad_size, grad.shape[-1]).norm(dim=2).bfloat16()
        Scheme.batch_whole_grad[:, self.grad_index_offset:self.grad_index_offset + self.grad_size] = grad_
        self.grad_updated = True
        # print("Set Scale:", id(Scheme._whole_grad_buffer_cat), "=====", id(Scheme.batch_whole_grad))



class Scheme_4D_new(Scheme):

    def scheme_init(self, mat_shape):
        assert len(mat_shape) == 4
        b, c, m, n = mat_shape
        self.grad_A_shape = (c, n)
        self.grad_B_shape = (c, m)
        self.grad_A_size = np.prod(self.grad_A_shape)
        self.grad_B_size = np.prod(self.grad_B_shape)
        Scheme._all_grad_A_shape.append(self.grad_A_shape)
        Scheme._all_grad_B_shape.append(self.grad_B_shape)
        self.grad_A_index_offset = copy.deepcopy(Scheme._grad_A_index_offset)
        self.grad_B_index_offset = copy.deepcopy(Scheme._grad_B_index_offset)
        Scheme._grad_A_index_offset += self.grad_A_size
        Scheme._grad_B_index_offset += self.grad_B_size
        self.inited = True
        #ipdb.set_trace()

    def get_scale(self):
        if self._whole_grad_buffer_cat is None: 
            return None, None
        else:
            bs = Scheme.batch_whole_grad_A.size(0)
            grad_A = Scheme.batch_whole_grad_A[:, self.grad_A_index_offset:self.grad_A_index_offset + self.grad_A_size]
            grad_B = Scheme.batch_whole_grad_B[:, self.grad_B_index_offset:self.grad_B_index_offset + self.grad_B_size]
            self.grad_updated = False
            # print("Get Grad Mean:", grad_A.mean(), grad_B.mean())
            return grad_A.view(bs, *self.grad_A_shape), grad_B.view(bs, *self.grad_B_shape)
        

    def set_scale(self, grad):
        assert grad.ndim == 4
        # print("Set Grad Mean:", grad.mean())
        # print("Grad Shape:", grad.shape)
        grad_A = grad.norm(dim=2).bfloat16() # self.grad_A[Scheme.batch] * 0.5 + grad.norm(dim=2) * 0.5
        grad_B = grad.norm(dim=3).bfloat16() # self.grad_B[Scheme.batch] * 0.5 + grad.norm(dim=3) * 0.5
        Scheme.batch_whole_grad_A[:, self.grad_A_index_offset:self.grad_A_index_offset + self.grad_A_size] = grad_A.view(-1, self.grad_A_size)
        Scheme.batch_whole_grad_B[:, self.grad_B_index_offset:self.grad_B_index_offset + self.grad_B_size] = grad_B.view(-1, self.grad_B_size)
        self.grad_updated = True


class Scheme_3D(Scheme):

    def scheme_init(self, mat_shape):
        assert len(mat_shape) == 3
        self.scales = torch.zeros((Scheme.num_samples, mat_shape[1]), dtype=torch.bfloat16, device='cpu')
        # self.scales = torch.zeros((Scheme.num_samples, mat_shape[1]), dtype=torch.bfloat16, device='cuda')
        self.inited = True

    def get_scale(self):
        if True:  # self.grad_updated:
            assert Scheme.batch is not None
            # scale = self.scales[Scheme.batch].clone()
            scale = self.scales[Scheme.batch]
            return scale.cuda().float()
        else:
            return None

    def set_scale(self, grad):
        assert Scheme.batch is not None
        # print("Grad Shape:", grad.shape)
        scale = grad.view(-1, self.scales.shape[1], grad.shape[-1]).norm(dim=-1)
        self.scales[Scheme.batch] = scale.bfloat16().cpu()
        # self.scales[Scheme.batch] = self.scales[Scheme.batch] * 0.5 + scale.bfloat16().cpu() * 0.5
        self.grad_updated = True
        
    # def set_scale(self, grad):
    #     assert Scheme.batch is not None
    #     # print("Grad Shape:", grad.shape)
    #     scale_cuda = grad.view(-1, self.scales.shape[1], grad.shape[-1]).norm(dim=-1).bfloat16()
        
    #     ####################################
        
    #     group = dist.new_group(list(range(Scheme.world_size)))
    #     index_tensor = torch.tensor(Scheme.batch, dtype=torch.int, device=scale_cuda.device)
    #     all_index_list = [torch.empty_like(index_tensor) for i in range(Scheme.world_size)]
    #     scale_list = [torch.empty_like(scale_cuda) for i in range(Scheme.world_size)]
    #     # torch.cuda.synchronize()
    #     dist.all_gather(scale_list, scale_cuda, group=group)
    #     # torch.cuda.synchronize()
    #     dist.all_gather(all_index_list, index_tensor, group=group)
    #     # torch.cuda.synchronize()
            
    #     for idx, batch in enumerate(all_index_list):
    #         self.scales[batch] = scale_list[idx].cpu()
        
    #     self.grad_updated = True


class Scheme_4D(Scheme):

    def scheme_init(self, mat_shape):
        assert len(mat_shape) == 4
        b, c, m, n = mat_shape
        self.grad_A = torch.zeros((Scheme.num_samples, c, n), dtype=torch.bfloat16, device='cpu')
        self.grad_B = torch.zeros((Scheme.num_samples, c, m), dtype=torch.bfloat16, device='cpu')
        # self.grad_A = torch.zeros((Scheme.num_samples, c, n), dtype=torch.bfloat16, device='cuda')
        # self.grad_B = torch.zeros((Scheme.num_samples, c, m), dtype=torch.bfloat16, device='cuda')
        self.inited = True

    def get_scale(self):
        if True:  # self.grad_updated: #
            assert Scheme.batch is not None
            grad_A = self.grad_A[Scheme.batch]
            grad_B = self.grad_B[Scheme.batch]
            self.grad_updated = False
            return grad_A.cuda(), grad_B.cuda()
        else:
            return None, None

    def set_scale(self, grad):
        assert grad.ndim == 4 and Scheme.batch is not None
        # print("Grad Shape:", grad.shape)
        self.grad_A[Scheme.batch] = grad.norm(dim=2).bfloat16().cpu()  # self.grad_A[Scheme.batch] * 0.5 + grad.norm(dim=2) * 0.5
        self.grad_B[Scheme.batch] = grad.norm(dim=3).bfloat16().cpu()  # self.grad_B[Scheme.batch] * 0.5 + grad.norm(dim=3) * 0.5
        # self.grad_A[Scheme.batch] = self.grad_A[Scheme.batch] * 0.5 + grad.norm(dim=2).bfloat16().cpu() * 0.5
        # self.grad_B[Scheme.batch] = self.grad_B[Scheme.batch] * 0.5 + grad.norm(dim=3).bfloat16().cpu() * 0.5
        self.grad_updated = True
    
    # def set_scale(self, grad):
    #     assert grad.ndim == 4 and Scheme.batch is not None
    #     print("Set Grad Mean:", grad.mean())
    #     grad_A_cuda = grad.norm(dim=2).bfloat16()  # self.grad_A[Scheme.batch] * 0.5 + grad.norm(dim=2) * 0.5
    #     grad_B_cuda = grad.norm(dim=3).bfloat16()  # self.grad_B[Scheme.batch] * 0.5 + grad.norm(dim=3) * 0.5
        
    #     ####################################
        
    #     group = dist.new_group(list(range(Scheme.world_size)))
    #     index_tensor = torch.tensor(Scheme.batch, dtype=torch.int, device=grad_A_cuda.device)
    #     all_index_list = [torch.empty_like(index_tensor) for i in range(Scheme.world_size)]
    #     grad_A_list = [torch.empty_like(grad_A_cuda) for i in range(Scheme.world_size)]
    #     grad_B_list = [torch.empty_like(grad_B_cuda) for i in range(Scheme.world_size)]
    #     # torch.cuda.synchronize()
    #     dist.all_gather(grad_A_list, grad_A_cuda, group=group)
    #     # torch.cuda.synchronize()
    #     dist.all_gather(grad_B_list, grad_B_cuda, group=group)
    #     # torch.cuda.synchronize()
    #     dist.all_gather(all_index_list, index_tensor, group=group)
    #     # torch.cuda.synchronize()
            
    #     for idx, batch in enumerate(all_index_list):
    #         self.grad_A[batch] = grad_A_list[idx].cpu()
    #         self.grad_B[batch] = grad_B_list[idx].cpu()
        
    #     self.grad_updated = True
        
