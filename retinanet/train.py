from math import isfinite
from statistics import mean

import torch
from apex import amp
from apex.parallel import DistributedDataParallel
from torch.optim import Adam
from torch.optim.lr_scheduler import ReduceLROnPlateau

from .backbones.layers import convert_fixedbn_model
from .dali import DaliDataIterator
from .data import DataIterator
from .infer import infer
from .utils import ignore_sigint, post_metrics, Profiler


def train(model, state, path, annotations, val_path, val_annotations, resize, max_size, jitter, batch_size, iterations,
          val_iterations, mixed_precision, lr, warmup, milestones, gamma, is_master=True, world=1, use_dali=True,
          verbose=True, metrics_url=None, logdir=None):
    'Train the model on the given dataset'

    # Prepare model
    nn_model = model
    stride = model.stride

    model = convert_fixedbn_model(model)
    if torch.cuda.is_available():
        model = model.cuda()

    # Setup optimizer and schedule
    # optimizer = SGD(model.parameters(), lr=lr, weight_decay=0.0001, momentum=0.9)
    optimizer = Adam(model.parameters(), lr=lr, weight_decay=0.0000001)

    model, optimizer = amp.initialize(model, optimizer,
                                      opt_level='O0' if mixed_precision else 'O0',
                                      # keep_batchnorm_fp32=True,
                                      loss_scale=128.0,
                                      verbosity=is_master)

    if world > 1:
        model = DistributedDataParallel(model)
    model.train()

    if 'optimizer' in state:
        optimizer.load_state_dict(state['optimizer'])

    '''
    def schedule(train_iter):
        if warmup and train_iter <= warmup:
            return 0.9 * train_iter / warmup + 0.1
        return gamma ** len([m for m in milestones if m <= train_iter])
    scheduler = LambdaLR(optimizer, schedule)
    '''

    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=2, verbose=True)

    # Prepare dataset
    if verbose: print('Preparing dataset...')
    data_iterator = (DaliDataIterator if use_dali else DataIterator)(
        path, jitter, max_size, batch_size, stride,
        world, annotations, training=True)
    if verbose: print(data_iterator)

    if verbose:
        print('    device: {} {}'.format(
            world, 'cpu' if not torch.cuda.is_available() else 'gpu' if world == 1 else 'gpus'))
        print('    batch: {}, precision: {}'.format(batch_size, 'mixed' if mixed_precision else 'full'))
        print('Training model for {} iterations...'.format(iterations))

    # Create TensorBoard writer
    if logdir is not None:
        from tensorboardX import SummaryWriter
        if is_master and verbose:
            print('Writing TensorBoard logs to: {}'.format(logdir))
        writer = SummaryWriter(logdir=logdir)

    profiler = Profiler(['train', 'fw', 'bw'])
    iteration = state.get('iteration', 0)
    try:
        while iteration < iterations:
            cls_losses, box_losses = [], []
            for i, (data, target) in enumerate(data_iterator):
                # scheduler.step(iteration)

                # Forward pass
                profiler.start('fw')

                optimizer.zero_grad()
                cls_loss, box_loss = model([data, target])
                del data
                profiler.stop('fw')

                # Backward pass
                profiler.start('bw')
                with amp.scale_loss(cls_loss + box_loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
                optimizer.step()

                # Reduce all losses
                cls_loss, box_loss = cls_loss.mean().clone(), box_loss.mean().clone()
                if world > 1:
                    torch.distributed.all_reduce(cls_loss)
                    torch.distributed.all_reduce(box_loss)
                    cls_loss /= world
                    box_loss /= world
                if is_master:
                    cls_losses.append(cls_loss)
                    box_losses.append(box_loss)

                if is_master and not isfinite(cls_loss + box_loss):
                    raise RuntimeError('Loss is diverging!\n{}'.format(
                        'Try lowering the learning rate.'))

                del cls_loss, box_loss
                profiler.stop('bw')

                iteration += 1
                profiler.bump('train')
                if is_master and (profiler.totals['train'] > 2 or iteration == iterations):
                    focal_loss = torch.stack(list(cls_losses)).mean().item()
                    box_loss = torch.stack(list(box_losses)).mean().item()
                    learning_rate = optimizer.param_groups[0]['lr']
                    if verbose:
                        msg = '[{:{len}}/{}]'.format(iteration, iterations, len=len(str(iterations)))
                        msg += ' focal loss: {:.5f}'.format(focal_loss)
                        msg += ', box loss: {:.5f}'.format(box_loss)
                        msg += ', {:.3f}s/{}-batch'.format(profiler.means['train'], batch_size)
                        msg += ' (fw: {:.3f}s, bw: {:.3f}s)'.format(profiler.means['fw'], profiler.means['bw'])
                        msg += ', {:.1f} im/s'.format(batch_size / profiler.means['train'])
                        msg += ', lr: {:.2g}'.format(learning_rate)
                        print(msg, flush=True)

                    if logdir is not None:
                        writer.add_scalar('focal_loss', focal_loss, iteration)
                        writer.add_scalar('box_loss', box_loss, iteration)
                        writer.add_scalar('learning_rate', learning_rate, iteration)
                        del box_loss, focal_loss

                    if metrics_url:
                        post_metrics(metrics_url, {
                            'focal loss': mean(cls_losses),
                            'box loss': mean(box_losses),
                            'im_s': batch_size / profiler.means['train'],
                            'lr': learning_rate
                        })

                    # Save model weights
                    state.update({
                        'iteration': iteration,
                        'optimizer': optimizer.state_dict(),
                        'scheduler': scheduler.state_dict(),
                    })
                    # with ignore_sigint():
                    #    nn_model.save(state)

                    profiler.reset()
                    del cls_losses[:], box_losses[:]

                if val_annotations and (iteration == iterations or iteration % val_iterations == 0):
                    f1_m = infer(model, val_path, None, resize, max_size, batch_size, annotations=val_annotations,
                                 mixed_precision=mixed_precision, is_master=is_master, world=world, use_dali=use_dali,
                                 is_validation=True, verbose=True)

                    if not isinstance(f1_m, str):
                        print('f1_m:' + str(f1_m))
                        scheduler.step(f1_m)
                    model.train()
                    if is_master:
                        print('Saving model: ' + str(state['iteration']))
                        with ignore_sigint():
                            nn_model.save(state)

                if iteration == iterations:
                    break
    except Exception as e:
        print(e)

    if logdir is not None:
        writer.close()
