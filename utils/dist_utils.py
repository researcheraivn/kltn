import pickle

import torch
import torch.distributed as dist
from utils.model_utils import move_to_device


def get_rank():
    if not dist.is_available():
        return -1
    if not dist.is_initialized():
        return -1
    return dist.get_rank()


def get_world_size():
    if not dist.is_available():
        return 1
    if not dist.is_initialized():
        return 1
    return dist.get_world_size()


def get_device():
    if not dist.is_available() or not dist.is_initialized():
        return torch.device("cuda", 0)
    else:
        return torch.device("cuda", get_rank())


def is_local_master():
    return get_rank() in [-1, 0]


def get_default_group():
    return dist.group.WORLD


def all_reduce(tensor, group=None):
    if group is None:
        group = get_default_group()
    return dist.all_reduce(tensor, group=group)


def all_gather_list(data, group=None, max_size=16384):
    """Gathers arbitrary data from all nodes into a list.
    Similar to :func:`~torch.distributed.all_gather` but for arbitrary Python
    data. Note that *data* must be picklable.
    Args:
        data (Any): data from the local worker to be gathered on other workers
        group (optional): group of the collective
    """
    SIZE_STORAGE_BYTES = 4  # int32 to encode the payload size

    enc = pickle.dumps(data)
    enc_size = len(enc)

    if enc_size + SIZE_STORAGE_BYTES > max_size:
        raise ValueError(
            f'encoded data exceeds max_size, this can be fixed by increasing '
            f'buffer size: {enc_size}')

    rank = get_rank()
    world_size = get_world_size()
    buffer_size = max_size * world_size

    if not hasattr(all_gather_list, '_buffer') or \
            all_gather_list._buffer.numel() < buffer_size:
        all_gather_list._buffer = torch.cuda.ByteTensor(buffer_size)
        all_gather_list._cpu_buffer = torch.ByteTensor(max_size).pin_memory()

    buffer = all_gather_list._buffer
    buffer.zero_()
    cpu_buffer = all_gather_list._cpu_buffer

    assert enc_size < 256 ** SIZE_STORAGE_BYTES, (
        f'Encoded object size should be less than {256 ** SIZE_STORAGE_BYTES} '
        f'bytes')

    size_bytes = enc_size.to_bytes(SIZE_STORAGE_BYTES, byteorder='big')

    cpu_buffer[0:SIZE_STORAGE_BYTES] = torch.ByteTensor(list(size_bytes))
    cpu_buffer[SIZE_STORAGE_BYTES: enc_size + SIZE_STORAGE_BYTES] = torch.ByteTensor(list(enc))

    start = rank * max_size
    size = enc_size + SIZE_STORAGE_BYTES
    buffer[start: start + size].copy_(cpu_buffer[:size])

    all_reduce(buffer, group=group)

    try:
        result = []
        for i in range(world_size):
            out_buffer = buffer[i * max_size: (i + 1) * max_size]
            size = int.from_bytes(out_buffer[0:SIZE_STORAGE_BYTES], byteorder='big')
            if size > 0:
                result.append(
                    pickle.loads(
                        bytes(
                            out_buffer[SIZE_STORAGE_BYTES: size+SIZE_STORAGE_BYTES].tolist())))
        return result
    except pickle.UnpicklingError:
        raise Exception(
            'Unable to unpickle data from other workers. all_gather_list requires all '
            'workers to enter the function together, so this error usually indicates '
            'that the workers have fallen out of sync somehow. Workers can fall out of '
            'sync if one of them runs out of memory, or if there are other conditions '
            'in your training script that can cause one worker to finish an epoch '
            'while other workers are still iterating over their portions of the data.')


def all_gather(data, to_cpu=True):
    world_size = get_world_size()
    if world_size == 1:
        data = torch.tensor(data)
        if to_cpu:
            data = data.cpu()
        return [data]

    device = get_device()

    if not torch.is_tensor(data):
        data = torch.Tensor(data)
    data = data.to(device)

    rest_size = data.size()[1:]

    local_size = torch.LongTensor([data.size(0)]).to(device)
    size_list = [torch.LongTensor([0]).to(device) for _ in range(world_size)]
    dist.all_gather(size_list, local_size)
    size_list = [int(size.item()) for size in size_list]
    
    # +1 for a weird thing happen when local size == max(size_list).
    max_size = max(size_list) + 1

    tensor_list = []
    for _ in size_list:
        tensor_list.append(torch.zeros(size=(max_size,)+rest_size).to(device))

    padding = torch.zeros(size=(max_size-local_size,)+rest_size).to(device)
    tensor = torch.cat((data, padding), dim=0)

    dist.all_gather(tensor_list, tensor)

    data_list = []
    for size, tensor in zip(size_list, tensor_list):
        data_list.append(tensor[:size])

    if to_cpu:
        data_list = move_to_device(data_list, 'cpu')

    return data_list