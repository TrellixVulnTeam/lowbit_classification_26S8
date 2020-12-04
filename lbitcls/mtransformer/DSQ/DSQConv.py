import torch
import torch.nn as nn
import torch.nn.functional as F
from ..builder import QUANLAYERS
from lbitcls.utils import get_rank

DEBUG = False
class RoundWithGradient(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        delta = torch.max(x) - torch.min(x)
        x = (x/delta + 0.5)
        return x.round() * 2 - 1
    @staticmethod
    def backward(ctx, g):
        return g 

@QUANLAYERS.register_module()
class DSQConv(nn.Conv2d):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, dilation=1, groups=1, bias=True,
                momentum = 0.1,                
                num_bit = 8, 
                QInput = True, 
                bSetQ = True,
                alpha_thres = 0.8):
        super(DSQConv, self).__init__(in_channels, out_channels, kernel_size, stride, padding, dilation, groups, bias)
        self.num_bit = num_bit
        self.quan_input = QInput
        self.bit_range = 2**self.num_bit -1	 
        self.is_quan = bSetQ        
        self.momentum = momentum
        self.alpha_thres = alpha_thres
        if self.is_quan:
            # using int32 max/min as init and backprogation to optimization
            # Weight
            self.uW = nn.Parameter(data = torch.tensor(3.0))
            self.lW  = nn.Parameter(data = torch.tensor(-3.0))
            self.register_buffer('running_uw', torch.tensor([self.uW.data])) # init with uw
            self.register_buffer('running_lw', torch.tensor([self.lW.data])) # init with lw
            self.alphaW = nn.Parameter(data = torch.tensor(0.2).float())
            # Bias
            if self.bias is not None:
                self.uB = nn.Parameter(data = torch.tensor(6.0))
                self.lB  = nn.Parameter(data = torch.tensor(-6.0))
                self.register_buffer('running_uB', torch.tensor([self.uB.data]))# init with ub
                self.register_buffer('running_lB', torch.tensor([self.lB.data]))# init with lb
                self.alphaB = nn.Parameter(data = torch.tensor(0.2).float())

            # Activation input		
            if self.quan_input:
                self.uA = nn.Parameter(data = torch.tensor(6.0))
                self.lA  = nn.Parameter(data = torch.tensor(-6.0))
                self.register_buffer('running_uA', torch.tensor([self.uA.data])) # init with uA
                self.register_buffer('running_lA', torch.tensor([self.lA.data])) # init with lA
                self.alphaA = nn.Parameter(data = torch.tensor(0.2).float())

    def clipping(self, x, lower, upper):
        x = x.clamp(lower.item(), upper.item())
        return x

    def floor_pass(self, x):
        y = torch.floor(x) 
        y_grad = x
        return y.detach() - y_grad.detach() + y_grad
        
    def phi_function(self, x, mi, alpha, delta):
        alpha = alpha.clamp(1e-6, self.alpha_thres - 1e-6)
        s = 1/(1-alpha)
        k = (2/alpha - 1).log() * (1/delta)
        x = (((x - mi) *k ).tanh()) * s 
        return x	

    def sgn(self, x):
        x = RoundWithGradient.apply(x)
        return x

    def dequantize(self, x, lower_bound, delta, interval):

        # save mem
        x =  ((x+1)/2 + interval) * delta + lower_bound

        return x

    def forward(self, x):
        if self.is_quan:
            if self.training:
                cur_running_lw = self.running_lw.mul(1-self.momentum).add((self.momentum) * self.lW)
                cur_running_uw = self.running_uw.mul(1-self.momentum).add((self.momentum) * self.uW)
            else:
                cur_running_lw = self.running_lw
                cur_running_uw = self.running_uw

            Qweight = self.clipping(self.weight, self.lW, self.uW)
            cur_min = self.lW
            cur_max = self.uW
            delta =  (cur_max - cur_min)/(self.bit_range)
            #interval = (Qweight - cur_min) //delta ## not differential ??
            interval = self.floor_pass((Qweight - cur_min) /delta)
            mi = (interval + 0.5) * delta + cur_min
            Qweight = self.phi_function(Qweight, mi, self.alphaW, delta)
            Qweight = self.sgn(Qweight)
            Qweight = self.dequantize(Qweight, cur_min, delta, interval)

            Qbias = self.bias
            # Bias			
            if self.bias is not None:
                if self.training:
                    cur_running_lB = self.running_lB.mul(1-self.momentum).add((self.momentum) * self.lB)
                    cur_running_uB = self.running_uB.mul(1-self.momentum).add((self.momentum) * self.uB)
                else:
                    cur_running_lB = self.running_lB
                    cur_running_uB = self.running_uB

                Qbias = self.clipping(self.bias, self.lB, self.uB)
                cur_min = self.lB
                cur_max = self.uB
                delta =  (cur_max - cur_min)/(self.bit_range)
                #interval = (Qbias - cur_min) //delta
                interval = self.floor_pass((Qbias - cur_min) /delta)
                mi = (interval + 0.5) * delta + cur_min
                Qbias = self.phi_function(Qbias, mi, self.alphaB, delta)
                Qbias = self.sgn(Qbias)
                Qbias = self.dequantize(Qbias, cur_min, delta, interval)

            # Input(Activation)
            Qactivation = x
            if self.quan_input:
                                
                if self.training:                    
                    cur_running_lA = self.running_lA.mul(1-self.momentum).add((self.momentum) * self.lA)
                    cur_running_uA = self.running_uA.mul(1-self.momentum).add((self.momentum) * self.uA)
                else:
                    cur_running_lA = self.running_lA
                    cur_running_uA = self.running_uA
                    
                Qactivation = self.clipping(x, self.lA, self.uA)
                cur_min = self.lA
                cur_max = self.uA
                delta =  (cur_max - cur_min)/(self.bit_range)
                #interval = (Qactivation - cur_min) //delta
                interval = self.floor_pass((Qactivation - cur_min) /delta)
                mi = (interval + 0.5) * delta + cur_min                
                Qactivation = self.phi_function(Qactivation, mi, self.alphaA, delta)
                Qactivation = self.sgn(Qactivation)
                Qactivation = self.dequantize(Qactivation, cur_min, delta, interval)
            
            output = F.conv2d(Qactivation, Qweight, Qbias,  self.stride, self.padding, self.dilation, self.groups)

        else:
            output =  F.conv2d(x, self.weight, self.bias, self.stride,
                        self.padding, self.dilation, self.groups)

        return output