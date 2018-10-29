import sys
sys.path.append('..')

import os
import time
import math
import scipy.io as sio
import matplotlib.pyplot as plt
import torch
from torch import nn, optim
from torch.utils.data import DataLoader
from multiprocessing import cpu_count

from ptb import PTB
from model import RNNVAE
from utils import linear_anneal


# device configuration
device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

# Penn TreeBank (PTB) dataset
data_path = '../data'
max_len = 96
splits = ['train', 'valid', 'test']
datasets = {split: PTB(root=data_path, split=split) for split in splits}

# data loader
batch_size = 32
dataloaders = {split: DataLoader(datasets[split],
                                 batch_size=batch_size,
                                 shuffle=split=='train',
                                 num_workers=cpu_count(),
                                 pin_memory=torch.cuda.is_available())
                                 for split in splits}
symbols = datasets['train'].symbols

# RNNVAE model
embedding_size = 300
hidden_size = 256
latent_dim = 16
dropout_rate = 0.5
model = RNNVAE(vocab_size=datasets['train'].vocab_size,
               embed_size=embedding_size,
               time_step=max_len,
               hidden_size=hidden_size,
               z_dim=latent_dim,
               dropout_rate=dropout_rate,
               bos_idx=symbols['<bos>'],
               eos_idx=symbols['<eos>'],
               pad_idx=symbols['<pad>'])
model = model.to(device)
print(model)

# folder to save model
save_path = 'model'
if not os.path.exists(save_path):
    os.makedirs(save_path)

# objective function
learning_rate = 0.001
criterion = nn.NLLLoss(size_average=False, ignore_index=symbols['<pad>'])
optimizer = optim.Adam(model.parameters(), lr=learning_rate)

# negative log likelihood
def NLL(logp, target, length):
    target = target[:, :torch.max(length).item()].contiguous().view(-1)
    logp = logp.view(-1, logp.size(-1))
    return criterion(logp, target)

# KL divergence
def KL_div(mu, logvar):
    return -0.5 * torch.sum(1. + logvar - mu.pow(2) - logvar.exp())

# training setting
epoch = 20
print_every = 50

# training interface
step = 0
tracker = {'ELBO': [], 'NLL': [], 'KL': [], 'KL_weight': []}
start_time = time.time()
for ep in range(epoch):
    # learning rate decay
    if ep >= 10 and ep % 2 == 0:
        learning_rate = learning_rate * 0.5
        for param_group in optimizer.param_groups:
            param_group['lr'] = learning_rate

    for split in splits:
        dataloader = dataloaders[split]
        model.train() if split == 'train' else model.eval()
        totals = {'ELBO': 0., 'NLL': 0., 'KL': 0., 'words': 0}

        for itr, (enc_inputs, dec_inputs, targets, lengths) in enumerate(dataloader):
            bsize = enc_inputs.size(0)
            enc_inputs = enc_inputs.to(device)
            dec_inputs = dec_inputs.to(device)
            targets = targets.to(device)
            lengths = lengths.to(device)

            # forward
            logp, mu, logvar = model(enc_inputs, dec_inputs, lengths)

            # calculate loss
            NLL_loss = NLL(logp, targets, lengths + 1)
            KL_loss = KL_div(mu, logvar)
            KL_weight = linear_anneal(step, len(dataloaders['train']) * 10)
            loss = (NLL_loss + KL_weight * KL_loss) / bsize

            # cumulate
            totals['ELBO'] += loss.item() * bsize
            totals['NLL'] += NLL_loss.item()
            totals['KL'] += KL_loss.item()
            totals['words'] += torch.sum(lengths).item()

            # backward and optimize
            if split == 'train':
                step += 1
                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 5)
                optimizer.step()

                # track
                tracker['ELBO'].append(loss.item())
                tracker['NLL'].append(NLL_loss.item() / bsize)
                tracker['KL'].append(KL_loss.item() / bsize)
                tracker['KL_weight'].append(KL_weight)

                # print statistics
                if itr % print_every == 0 or itr + 1 == len(dataloader):
                    print("%s Batch %04d/%04d, ELBO-Loss %.4f, "
                          "NLL-Loss %.4f, KL-Loss %.4f, KL-Weight %.4f"
                          % (split.upper(), itr, len(dataloader),
                             tracker['ELBO'][-1], tracker['NLL'][-1],
                             tracker['KL'][-1], tracker['KL_weight'][-1]))

        samples = len(datasets[split])
        print("%s Epoch %02d/%02d, ELBO %.4f, NLL %.4f, KL %.4f, PPL %.4f"
              % (split.upper(), ep, epoch, totals['ELBO'] / samples,
                 totals['NLL'] / samples, totals['KL'] / samples,
                 math.exp(totals['NLL'] / totals['words'])))

    # save checkpoint
    checkpoint_path = os.path.join(save_path, "E%02d.pkl" % ep)
    torch.save(model.state_dict(), checkpoint_path)
    print("Model saved at %s\n" % checkpoint_path)
end_time = time.time()
print('Total cost time',
      time.strftime("%H hr %M min %S sec", time.gmtime(end_time - start_time)))

# plot KL curve
fig, ax1 = plt.subplots()
lns1 = ax1.plot(tracker['KL_weight'], 'b', label='KL term weight')
ax1.set_ylim([-0.05, 1.05])
ax1.set_xlabel('Step')
ax1.set_ylabel('KL term weight')
ax2 = ax1.twinx()
lns2 = ax2.plot(tracker['KL'], 'r', label='KL term value')
ax2.set_ylabel('KL term value')
lns = lns1 + lns2
labs = [l.get_label() for l in lns]
ax1.legend(lns, labs, bbox_to_anchor=(0., 1.02, 1., .102),
           ncol=2, mode="expand", borderaxespad=0.)
# plt.show()

# save learning results
sio.savemat("results.mat", tracker)
