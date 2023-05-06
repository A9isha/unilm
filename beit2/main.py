import argparse
import os
import torch
import torch.distributed as dist
from torch.utils.data import Dataset, DataLoader
import torch_xla.core.xla_model as xm
import torch_xla.distributed.parallel_loader as pl
import torch_xla.distributed.xla_multiprocessing as xmp
from torch_xla.debug.metrics import metrics_report
import torch_xla.debug.profiler as xp

from beit2.beit_model import BeitTrainingModule
from beit2.beit_local_dataset import BeitLocalDataset
from beit2.beit_v2_loss import BeitV2Loss

from itertools import chain
from typing import Dict, Union

import torch_xla.experimental.pjrt_backend

patch_size = 16
image_key = "img"
img_shape = (224, 224)
NH = img_shape[0]//patch_size
NW = img_shape[1]//patch_size


def synchronize(message: str = "sync-workers"):
    xm.rendezvous(message)

def broadcast_params(model, root_rank: int = 0):
    parameters_and_buffers = []
    for p in chain(model.parameters(), model.buffers()):
    # Set all params in non-master devices to zero so that all_reduce is
    # equivalent to broadcasting parameters from master to other devices.
        if xm.get_ordinal() != root_rank:
            zero = torch.tensor(0, dtype=p.data.dtype, device=p.data.device)
            p.data.mul_(zero)
            parameters_and_buffers.append(p.data)
    xm.wait_device_ops()
    xm.all_reduce(xm.REDUCE_SUM, parameters_and_buffers)
    xm.mark_step()
    # synchronize("broadcast_xla_master_model_param")


def mp_fn(local_rank):
    device = xm.xla_device()
    print(device)
    # dist.init_process_group('xla', init_method='pjrt://')
    print(f"xm.xrt_world_size()={xm.xrt_world_size()}")
    mae_model_config = {'patch_size': patch_size, 'in_chans': 1,
                        'depth': 12, 'num_heads': 12, 'embed_dim': 768,
                        'decoder_depth': 2, 'decoder_num_heads': 16, 'decoder_embed_dim': 512}

    model = BeitTrainingModule(mae_model_config=mae_model_config)
    loss = BeitV2Loss()
    # print(f"loss={loss}")
    broadcast_params(model)
    # print(f"broadcasted params")
    optimizer = torch.optim.AdamW(params=model.parameters(), amsgrad=False)


    num_params = sum(p.numel() for p in model.parameters())
    print(f'built model with {num_params / 1e6}M params')
    batch_size=128
    dl = pl.MpDeviceLoader(DataLoader(BeitLocalDataset(batch_size * 10000), batch_size=batch_size, num_workers=10), device)
    import time
    model.train()
    log_interval=10
    t0 = time.perf_counter()

    model.to(device)
    # optimizer.to(device) # doesnt work
    loss.to(device)
#    for batch_idx in range(1, 1001):

    server = xp.start_server(9229)
    for batch_idx, batch_data in enumerate(dl, start=1):
        with xp.StepTrace('train_beit2'):
            with xp.Trace('build_graph'):        
                optimizer.zero_grad(set_to_none=True)

                if isinstance(batch_data, dict):
                    for k, v in batch_data.items():
                        batch_data[k] = v.to(device)
                else:
                    batch_data = batch_data.to(device)
                outputs = model(batch_data)
                l = loss(outputs, batch_data)['loss']

            l.backward()
            xm.optimizer_step(optimizer)
            print(f"processed batch_idx={batch_idx}")
            if batch_idx % log_interval == 0 and local_rank == 0:
                time_passed = time.perf_counter() - t0
                samples_processed = xm.xrt_world_size() * batch_size * log_interval
                print(f'{samples_processed / time_passed} samples/second')
                with open('/tmp/beit_vanilla_met_report.txt', 'w') as f:
                        f.write(metrics_report())

                t0 = time.perf_counter()

if __name__ == '__main__':
    # if args.pjrt_distributed:
    
    
    # print(f"initially xm.xrt_world_size()={xm.xrt_world_size()}")
    # main()
    # nprocs = 1 
    xmp.spawn(mp_fn,
              args=()#,
            #   nprocs=nprocs
              )
