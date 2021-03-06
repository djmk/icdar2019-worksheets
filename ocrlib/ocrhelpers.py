import numpy as np
from numpy import *
from scipy import ndimage as ndi
import torch
from functools import wraps
import editdistance
from torchmore import flex, layers
from torch import optim, nn
import torch.nn.functional as F
import sys, os
import time
import IPython

import matplotlib.pyplot as plt
plt.rc("image", cmap="gray")
plt.rc("image", interpolation="nearest")
import scipy.ndimage as ndi

def RUN(x):
    """Run a command and output the result."""
    print(x, ":", os.popen(x).read().strip())
    
def scale_to(a, shape):
    """Scale a numpy array to a given target size."""
    scales = array(a.shape, "f") / array(shape, "f")
    result = ndi.affine_transform(a, diag(scales), output_shape=shape, order=1)
    return result

def tshow(a, order, b=0, ax=None, **kw):
    """Display a torch array with imshow."""
    from matplotlib.pyplot import gca
    ax = ax or gca()
    if set(order)==set("BHWD"):
        a = layers.reorder(a.detach().cpu(), order, "BHWD")[b].numpy()
    elif set(order)==set("HWD"):
        a = layers.reorder(a.detach().cpu(), order, "HWD").numpy()
    elif set(order)==set("HW"):
        a = layers.reorder(a.detach().cpu(), order, "HW").numpy()
    else:
        raise ValueError(f"{order}: unknown order")
    if a.shape[-1]==1: a = a[...,0]
    ax.imshow(a, **kw)

def asnp(a):
    """Convert to numpy."""
    if isinstance(a, torch.Tensor):
        return a.detach().cpu().numpy()
    else:
        assert isinstance(a, np.ndarray)
        return a

def method(cls):
    """A decorator allowing methods to be added to classes."""
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            return func(self, *args, **kwargs)
        setattr(cls, func.__name__, wrapper)
        return func
    return decorator

def ctc_decode(probs, sigma=1.0, threshold=0.7, kind=None, full=False):
    """A simple decoder for CTC-trained OCR recognizers.

    :probs: d x l sequence classification output
    """
    probs = asnp(probs.T)
    assert (abs(probs.sum(1)-1) < 1e-4).all(), \
        "input not normalized; did you apply .softmax()?"
    probs = ndi.gaussian_filter(probs, (sigma, 0))
    probs /= probs.sum(1)[:,newaxis]
    labels, n = ndi.label(probs[:,0]<threshold)
    mask = tile(labels[:,newaxis], (1, probs.shape[1]))
    mask[:,0] = 0
    maxima = ndi.maximum_position(probs, mask, arange(1, amax(mask)+1))
    if not full:
        return [c for r, c in sorted(maxima)]
    else:
        return [(r, c, probs[r, c]) for r, c in sorted(maxima)]
    
def pack_for_ctc(seqs):
    """Pack a list of sequences for nn.CTCLoss."""
    allseqs = torch.cat(seqs).long()
    alllens = torch.tensor([len(s) for s in seqs]).long()
    return (allseqs, alllens)

def collate4ocr(samples):
    """Collate image+sequence samples into batches.

    This returns an image batch and a compressed sequence batch using CTCLoss conventions.
    """
    images, seqs = zip(*samples)
    images = [im.unsqueeze(2) if im.ndimension()==2 else im for im in images]
    w, h, d = map(max, zip(*[x.shape for x in images]))
    result = torch.zeros((len(images), w, h, d), dtype=torch.float)
    for i, im in enumerate(images):
        w, h, d = im.shape
        if im.dtype == torch.uint8:
            im = im.float() / 255.0
        result[i, :w, :h, :d] = im
    allseqs = torch.cat(seqs).long()
    alllens = torch.tensor([len(s) for s in seqs]).long()
    return (result, (allseqs, alllens))

def model_device(model):
    """Find the device of a model."""
    return next(model.parameters()).device

device = None

def get_maxcount(dflt=999999999):
    """Get maxcount from a file if available."""
    if os.path.exists("__MAXCOUNT__"):
        with open("__MAXCOUNT__") as stream:
            maxcount = int(stream.read().strip())
        print(f"__MAXCOUNT__ {maxcount}", file=sys.stderr)
    else:
        maxcount = int(os.environ.get("maxcount", dflt))
        if maxcount != dflt:
            print(f"maxcount={maxcount}", file=sys.stderr)
    return maxcount

def CTCLossBDL(log_softmax=True):
    """Compute CTC Loss on BDL-order tensors.

    This is a wrapper around nn.CTCLoss that does a few things:
    - it accepts the output as a plain tensor (without lengths)
    - it forforms a softmax
    - it accepts output tensors in BDL order (regular CTC: LBD)
    """
    ctc_loss = nn.CTCLoss()
    def lossfn(outputs, targets):
        assert isinstance(targets, tuple) and len(targets)==2
        layers.check_order(outputs, "BDL")
        b, d, l = outputs.size()
        olens = torch.full((b,), l).long()
        if log_softmax: outputs = outputs.log_softmax(1)
        outputs = layers.reorder(outputs, "BDL", "LBD")
        targets, tlens = targets
        assert tlens.size(0)==b
        assert tlens.sum()==targets.size(0)
        return ctc_loss(outputs.cpu(), targets.cpu(), olens.cpu(), tlens.cpu())
    return lossfn

def softmax1(x):
    """Softmax on second dimension."""
    return x.softmax(1)

class SavingForTrainer(object):
    """Saving mixin for Trainers."""
    def __init__(self):
        super().__init__()
        self.savedir = os.environ.get("savedir", "./models")
        self.loss_horizon = 100
        self.loss_scale = 1.0

    def save_epoch(self, epoch):
        if not hasattr(self.model, "model_name"): return
        if not self.savedir or self.savedir=="": return
        if not os.path.exists(self.savedir): return
        if not hasattr(self, "losses") or len(self.losses)<self.loss_horizon: return
        base = self.model.model_name
        ierr = int(1e6*mean(self.losses[-self.loss_horizon:])*self.loss_scale)
        ierr = min(999999999, ierr)
        loss = "%09d" % ierr
        epoch = "%03d"%epoch
        fname = f"{self.savedir}/{base}-{epoch}-{loss}.pth"
        print(f"saving {fname}", file=sys.stderr)
        torch.save(self.model.state_dict(), fname)   

    def load(self, fname):
        print(f"loading {fname}", file=sys.stderr)
        self.model.load_state_dict(torch.load(fname))

    def load_best(self):
        import glob
        assert hasattr(self.model, "model_name")
        pattern = f"{self.savedir}/{self.model.model_name}-*.pth"
        files = glob.glob(pattern)
        assert len(files)>0, f"no {pattern} found"
        def lossof(fname): 
            return fname.split(".")[-2].split("-")[-1]
        files = sort(files, key=lossof)
        fname = files[-1]
        self.load(fname)

class ReporterForTrainer(object):
    """Report mixin for Trainers."""
    def __init__(self):
        super().__init__()
        self.last_display = time.time()-999999

    def report_simple(self):
        avgloss = mean(self.losses[-100:]) if len(self.losses)>0 else 0.0
        print(f"{self.epoch:3d} {self.count:9d} {avgloss:10.4f}", " "*10, file=sys.stderr, end="\r", flush=True)

    def report_end(self):
        if int(os.environ.get("noreport", 0)): return
        from IPython import display
        display.clear_output(wait=True)

    def report_inputs(self, ax, inputs):
        ax.set_title(f"{self.epoch} {self.count}")
        ax.imshow(inputs[0,0].detach().cpu(), cmap="gray")

    def report_losses(self, ax, losses):
        if len(losses) < 100: return
        losses = ndi.gaussian_filter(losses, 10.0)
        losses = losses[::10]
        losses = ndi.gaussian_filter(losses, 10.0)
        ax.plot(losses)
        ax.set_ylim((0.9*amin(losses), median(losses)*3))

    def report_outputs(self, ax, outputs):
        pass

    def report(self):
        import matplotlib.pyplot as plt
        from IPython import display
        if int(os.environ.get("noreport", 0)): return
        if time.time()-self.last_display < self.every: return
        self.last_display = time.time()
        plt.close("all")
        fig = plt.figure(figsize=(10, 8))
        fig.clf()
        inputs, targets, outputs = self.last_batch
        if hasattr(self, "report_extra"):
            for i in range(4): fig.add_subplot(2, 2, i+1)
            ax1, ax2, ax3, ax4 = fig.get_axes()
            self.report_extra(ax4, inputs, targets, outputs)
        else:
            for i in range(3): fig.add_subplot(3, 1, i+1)
            ax1, ax2, ax3 = fig.get_axes()
        self.report_inputs(ax1, inputs)
        self.report_outputs(ax2, outputs)
        self.report_losses(ax3, self.losses)
        display.clear_output(wait=True)
        display.display(fig)


class BaseTrainer(ReporterForTrainer, SavingForTrainer):
    def __init__(self, model, *, lossfn=None, probfn=softmax1, lr=1e-4, every=3.0, device=None, savedir=True, maxgrad = 10.0, **kw):
        super().__init__()
        self.model = model
        self.device = None
        #self.lossfn = nn.CTCLoss()
        self.lossfn = lossfn
        self.probfn = probfn
        self.every = every
        self.losses = []
        self.last_lr = None
        self.set_lr(lr)
        self.clip_gradient = maxgrad
        self.charset = None
        self.maxcount = get_maxcount()

    def set_lr(self, lr, momentum=0.9):
        """Set the learning rate.

        Keeps track of current learning rate and only allocates a new optimizer if it changes."""
        if lr!=self.last_lr:
           self.optimizer = optim.SGD(self.model.parameters(), lr=lr, momentum=momentum)
           self.last_lr = lr 

    def train_batch(self, inputs, targets):
        """All the steps necessary for training a batch.

        Stores the last batch in self.last_batch.
        Adds the loss to self.losses.
        Clips the gradient if self.clip_gradient is not None.
        """
        self.model.train()
        self.optimizer.zero_grad()
        if self.device is not None:
            inputs = inputs.to(device)
        outputs = self.model.forward(inputs)
        assert inputs.size(0) == outputs.size(0)
        loss = self.compute_loss(outputs, targets)
        loss.backward()
        if self.clip_gradient is not None:
            nn.utils.clip_grad_norm_(self.model.parameters(), self.clip_gradient)
        self.optimizer.step()
        self.last_batch = (inputs, targets, outputs)
        return loss.detach().item()

    def compute_loss(self, outputs, targets):
        """Call the loss function. Override for special cases."""
        return self.lossfn(outputs, targets)

    def probs_batch(self, inputs):
        """Compute probability outputs for the batch. Uses `probfn`."""
        self.model.eval()
        with torch.no_grad():
            outputs = self.model.forward(inputs.to(self.device))
        return self.probfn(outputs.detach().cpu())

    def train(self, loader, epochs=1, learning_rates=None, total=None, cont=False, every=None):
        """Train over a dataloader for the given number of epochs."""
        if every: self.every = every
        if learning_rates is None:
            learning_rates = [self.last_lr] * epochs
        for epoch, lr in enumerate(learning_rates):
            self.set_lr(lr)
            self.epoch = epoch
            self.count = 0
            for sample in loader:
                images, targets = sample
                loss = self.train_batch(images, targets)
                self.report()
                self.losses.append(float(loss))
                self.count += 1
                if len(self.losses) >= self.maxcount:
                    break
            if len(self.losses) >= self.maxcount:
                break
            self.save_epoch(epoch)
        self.report_end()


class LineTrainer(BaseTrainer):
    """Specialized Trainer for training line recognizers with CTC."""
    def __init__(self, model, **kw):
        super().__init__(model, lossfn=CTCLossBDL(), **kw)

    def report_outputs(self, ax, outputs):
        """Plot the posteriors for each class and location."""
        layers.check_order(outputs, "BDL")
        pred = outputs[0].detach().cpu().softmax(0).numpy()
        for i in range(pred.shape[0]):
            ax.plot(pred[i])

    def errors(self, loader):
        """Compute OCR errors using edit distance."""
        total = 0
        errors = 0
        for inputs, targets in loader:
            targets, tlens = targets
            predictions = self.predict_batch(inputs)
            start = 0
            for p, l in zip(predictions, tlens):
                t = targets[start:start+l].tolist()
                errors += editdistance.distance(p, t)
                total += len(t)
                start += l
                if total > self.maxcount: break
            if total > self.maxcount: break
        return errors, total

    def predict_batch(self, inputs, **kw):
        """Predict and decode a batch."""
        probs = self.probs_batch(inputs)
        result = [ctc_decode(p, **kw) for p in probs]
        return result

class SegTrainer(BaseTrainer):
    """Segmentation trainer: image to pixel classes."""
    def __init__(self, model, margin=16, **kw):
        """LIke regular trainer but allows margin specification."""
        super().__init__(model, lossfn=nn.CrossEntropyLoss(), **kw)
        self.margin = margin

    def compute_loss(self, outputs, targets):
        """Compute loss taking a margin into account."""
        b, d, h, w = outputs.shape
        b1, h1, w1 = targets.shape
        assert h<=h1 and w<=w1 and h1-h<5 and w1-w<5, (outputs.shape, targets.shape)
        targets = targets[:,:h,:w]
        #lsm = outputs.log_softmax(1)
        if self.margin > 0:
            m = self.margin
            outputs = outputs[:,:,m:-m,m:-m]
            targets = targets[:,m:-m,m:-m]
        loss = self.lossfn(outputs, targets.to(outputs.device))
        return loss

    def report_outputs(self, ax, outputs):
        """Display the RGB output posterior probabilities."""
        from IPython import display
        p = outputs.detach().cpu().softmax(1)
        b, d, h, w = outputs.size()
        result = asnp(p)[0].transpose(1, 2, 0)
        result -= amin(result)
        result /= amax(result)
        ax.imshow(result)
        ax.plot([w//2, w//2], [0, h], color="white", alpha=0.5)

    def report_extra(self, ax, inputs, targets, outputs):
        from IPython import display
        p = outputs.detach().cpu().softmax(1)
        b, d, h, w = p.size()
        colors = "r g b".split()
        for i in range(d):
            ax.plot(p[0, i, :, w//2], color=colors[i])
