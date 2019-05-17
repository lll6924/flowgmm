import torch
import torch.nn.functional as F
import torch.nn as nn
import numpy as np
import scipy as sp
import scipy.sparse
from .normalizations import pad_circular_nd,flip
from flow_ssl.utils import export
from flow_ssl.conv_parts import conv2d


@export
class iConv2d(nn.Module):
    """ wraps conv2d in a module with an inverse function """
    def __init__(self,*args,inverse_tol=1e-7,circ=True,**kwargs):
        super().__init__()
        self.conv = conv2d(*args,**kwargs)
        self.inverse_tol = inverse_tol
        self._reverse_iters = 0
        self._inverses_evaluated = 0
        self._circ= circ
    @property
    def iters_per_reverse(self):
        return self._reverse_iters/self._inverses_evaluated
    def forward(self,x):
        self._shape = x.shape
        if self._circ:
            padded_x = pad_circular_nd(x,1,dim=[2,3])
            return F.conv2d(padded_x,self.conv.weight,self.conv.bias)
        else:
            return self.conv(x)
    # FFT inverse method
    def inverse(self,y):
        x = inverse_fft_conv3x3_pytorch(y-self.conv.bias[None,:,None,None],self.conv.weight)
        return x
    def logdet(self):
        bs,c,h,w = self._shape
        padded_weight = F.pad(self.conv.weight,(0,h-3,0,w-3))
        w_fft = torch.rfft(padded_weight, 2, onesided=False, normalized=False)
        # Lift to real valued space
        D = phi(w_fft).permute(2,3,0,1)
        Dt = D.permute(0, 1, 3, 2) #transpose of D
        lhs = torch.matmul(D, Dt)
        chol_output = torch.cholesky(lhs+1e-5*torch.eye(lhs.size(-1)).to(lhs.device))
        eigs = torch.diagonal(chol_output,dim1=-2,dim2=-1)
        logdet = (eigs.log().sum() / 2.0).expand(bs)
        # 1/4 \sum_{h,w} log det (DDt)
        return logdet

@export
class iConv1x1(nn.Conv2d):
    def __init__(self, channels):
        super().__init__(channels,channels,1)

    def logdet(self):
        bs,c,h,w = self._input_shape
        return (torch.slogdet(self.weight[:,:,0,0])[1]*h*w).expand(bs)
    def inverse(self,y):
        bs,c,h,w = self._input_shape
        inv_weight = torch.inverse(self.weight[:,:,0,0].double()).float().view(c, c, 1, 1)
        debiased_y = y - self.bias[None,:,None,None]
        x = F.conv2d(debiased_y,inv_weight)
        return x

    def forward(self, x):
        self._input_shape = x.shape
        return F.conv2d(x,self.weight,self.bias)

@export
class iCoordInjection(nn.Module):
    def __init__(self,out_channels,mid_channels=8):
        super().__init__()
        self.mul_net = nn.Sequential(conv2d(0,mid_channels,coords=True),
                                nn.ReLU(),conv2d(mid_channels,out_channels,coords=True))
        self.bias_net = nn.Sequential(conv2d(0,mid_channels,coords=True),
                                nn.ReLU(),conv2d(mid_channels,out_channels,coords=True))
        
    def forward(self,x):
        bs,c,h,w = x.shape
        empty_input = torch.zeros(bs,0,h,w).to(x.device)
        log_mul = self.mul_net(empty_input)
        mul = torch.exp(log_mul)
        bias = self.bias_net(empty_input)
        self._log_mul = log_mul
        return x*mul + bias
    def inverse(self,y):
        bs,c,h,w = y.shape
        empty_input = torch.zeros(bs,0,h,w).to(y.device)
        mul = torch.exp(self.mul_net(empty_input))
        bias = self.bias_net(empty_input)
        return (y - bias)/mul
    def logdet(self):
        return self._log_mul.sum(3).sum(2).sum(1)

def fft_conv3x3(x,weight):
    bs,c,h,w = x.shape
    # Transform x to fourier space
    input_np = x.permute((2,3,1,0)).cpu().data.numpy()
    padded_input = np.pad(input_np,((1,1),(1,1),(0,0),(0,0)),mode='constant')
    fft_input = np.fft.fft2(padded_input, axes=[0,1])
    # Transform weights to fourier
    weight_np = weight.detach().cpu().permute((2,3,0,1)).numpy()
    padded_numpy = np.pad(weight_np,(((w-1)//2,(w-1)//2+(w-1)%2),((w-1)//2,(w-1)//2+(w-1)%2),(0,0),(0,0)),mode='constant')
    kernel_fft = np.conj(np.fft.fft2(padded_numpy, axes=[0,1]))
    u,sigma,vh = np.linalg.svd(kernel_fft)

    # Apply filter in fourier space
    filtered = (u@((sigma[...,None]*vh)@fft_input))
    # Transform back to spatial domain appropriately shifting
    output = np.real(np.fft.fftshift(np.fft.ifft2(filtered,axes=[0,1]),axes=[0,1]).transpose((3,2,0,1)))[...,1:h+1,1:w+1]
    return torch.from_numpy(output).float().to(x.device)

def inverse_fft_conv3x3(x,weight):
    bs,c,h,w = x.shape
    # Transform x to fourier space
    input_np = x.permute((2,3,1,0)).cpu().data.numpy()
    fft_input = np.fft.fft2(input_np, axes=[0,1])
    #print('np_fft_input',fft_input)
    # Transform weights to fourier
    weight_np = weight.detach().cpu().permute((2,3,0,1)).numpy()
    padded_numpy = np.pad(weight_np,(((w-3)//2,(w-3)//2+(w-3)%2),((w-3)//2,(w-3)//2+(w-3)%2),(0,0),(0,0)),mode='constant')
    kernel_fft = np.conj(np.fft.fft2(padded_numpy.astype(np.float64),axes=[0,1]))
    #print('np_padded_weight',padded_numpy)
    #print('np_kernel_fft',kernel_fft)
    W_fft_inv = np.linalg.inv(kernel_fft)
    filtered = (W_fft_inv@fft_input)
    # if np.any(np.isnan(filtered)):
    #     u,sigma,vh = np.linalg.svd(kernel_fft)
    #     assert False, f"Lowest singular value is {np.min(sigma.reshape(-1))}, {np.max(np.abs(input_np.reshape(-1)))}"
    # u,sigma,vh = np.linalg.svd(kernel_fft)#'=
    # v,uh = vh.conj().transpose((0,1,3,2)),u.conj().transpose((0,1,3,2))
    # # Apply filter in fourier space
    # filtered = (v@((uh/sigma[...,None])@fft_input))#.astype(np.float32)
    # Transform back to spatial domain appropriately shifting
    output = np.real(np.fft.ifft2(filtered,axes=[0,1]).transpose((3,2,0,1))).astype(np.float32)#[...,1:h+1,1:w+1]
    output = np.roll(np.roll(output,-((h-1)//2),-2),-((w-1)//2),-1)
    return torch.from_numpy(output).float().to(x.device)


def inverse_fft_conv3x3_pytorch(x,weight):
    bs,c,h,w = x.shape
    # Transform x to fourier space
    fft_input = torch.rfft(x,2,onesided=False,normalized=False)
    phi_fft_input = phi_vec(fft_input).permute(2,3,1,0) #h,w,c,bs

    # Transform weights to fourier #(flip the filter because cross-correlation not convolution)
    padded_weight = F.pad(weight,((w-3)//2,(w-3)//2+(w-3)%2,(w-3)//2,(w-3)//2+(w-3)%2))
    padded_weight[...,1]*=-1 #complex conjugate #(something wrong with pytorch here, doesn't make difference)
    fft_weight = torch.rfft(padded_weight,2,onesided=False,normalized=False)
    phi_fft_weight = phi(fft_weight)
    inverse_phi_fft_weight = torch.inverse(phi_fft_weight.permute(2,3,0,1)) #h,w,c,c

    # compute the product
    product = phi_inv_vec((inverse_phi_fft_weight@phi_fft_input).permute(3,2,0,1)) #bs,c,h,w
    conv_inverse  = torch.irfft(product,2,onesided=False,normalized=False)
    unshifted = torch.roll(torch.roll(conv_inverse,-((h-1)//2),-2),-((w-1)//2),-1)
    return unshifted

def phi(C):
    """ Computes the Reallification [[A, -B],[B,A]] for the complex matrix C=A+iB,
        assumes that A = C[...,0] and B = C[...,1], also assumes C is c x c x h x w x 2"""
    A = C[...,0]
    B = C[...,1]
    D = torch.cat([ torch.cat([ A, B],dim=1), 
                    torch.cat([-B, A],dim=1)], dim=0)
    return D

def phi_inv(D):
    """ Inverse of the reallification phi"""
    AB,_ = torch.chunk(D,2,dim=0)
    A,B = torch.chunk(AB,2,dim=1)
    return torch.stack([A,B],dim=len(D.shape))

def phi_vec(v):
    """Realification for complex vectors v"""
    a = v[...,0]
    b = v[...,1]
    return torch.cat([ a, b],dim=1)
def phi_inv_vec(v):
    """ inverse reallification for vectors"""
    a,b = torch.chunk(v,2,dim=1)
    return torch.stack([a,b],dim=len(v.shape))
