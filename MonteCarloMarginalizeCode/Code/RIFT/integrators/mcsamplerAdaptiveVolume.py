# mcsamplerAdaptiveVolume
#
# Algorithm: based on Tiwari VARAHA https://arxiv.org/pdf/2303.01463.pdf
# Based strongly on 'varaha_example.ipynb' email from 2023/03/07


import sys
import math
#import bisect
from collections import defaultdict

import numpy
np=numpy #import numpy as np
from scipy import integrate, interpolate, special
import itertools
import functools

import os

try:
  import cupy
  import cupyx   # needed for logsumexp
  xpy_default=cupy
  try:
    xpy_special_default = cupyx.scipy.special
    if not(hasattr(xpy_special_default,'logsumexp')):
          print(" mcsamplerAV: no cupyx.scipy.special.logsumexp, fallback mode ...")
          xpy_special_default= special
  except:
    print(" mcsamplerAV: no cupyx.scipy.special, fallback mode ...")
    xpy_special_default= special
  identity_convert = cupy.asnumpy
  identity_convert_togpu = cupy.asarray
  junk_to_check_installed = cupy.array(5)  # this will fail if GPU not installed correctly
  cupy_ok = True
  cupy_pi = cupy.array(np.pi)

  from RIFT.interpolators.interp_gpu import interp

#  from logging import info as log
#  import inspect
#  def verbose_cupy_asarray(*args, **kwargs):
#     print("Transferring data to VRAM", *args, **kwargs)
#     return cupy.asarray(*args, **kwargs)
#  def verbose_cupy_asnumpy(*args, **kwargs):
#     curframe = inspect.currentframe()
#     calframe = inspect.getouterframes(curframe, 2)
#     log("Transferring data to RAM",calframe[1][3]) #,args[0].__name__) #, *args, **kwargs)
#     return cupy.ndarray.asnumpy(*args, **kwargs)
#  cupy.asarray = verbose_cupy_asarray  
#  cupy.ndarray.asnumpy = verbose_cupy_asnumpy

except:
  print(' no cupy (mcsamplerAV)')
#  import numpy as cupy  # will automatically replace cupy calls with numpy!
  xpy_default=numpy  # just in case, to make replacement clear and to enable override
  xpy_special_default = special
  identity_convert = lambda x: x  # trivial return itself
  identity_convert_togpu = lambda x: x
  cupy_ok = False
  cupy_pi = np.pi

def set_xpy_to_numpy():
   xpy_default=numpy
   identity_convert = lambda x: x  # trivial return itself
   identity_convert_togpu = lambda x: x
   cupy_ok = False
   

if 'PROFILE' not in os.environ:
   def profile(fn):
        return fn

if not( 'RIFT_LOWLATENCY'  in os.environ):
    # Dont support selected external packages in low latency
 try:
    import healpy
 except:
    print(" - No healpy - ")

from RIFT.integrators.statutils import  update,finalize, init_log,update_log,finalize_log

#from multiprocessing import Pool

from RIFT.likelihood import vectorized_general_tools

__author__ = "Chris Pankow <pankow@gravity.phys.uwm.edu>, Dan Wysocki, R. O'Shaughnessy"

rosDebugMessages = True

class NanOrInf(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

### V. Tiwari routines

def get_likelihood_threshold(lkl, lkl_thr, nsel, discard_prob):
    """
    Find the likelihood threshold that encolses a probability
    lkl  : array of likelihoods (on bins)
    lkl_thr: scalar cutoff
    nsel : integer, has to do with size of array of likelihoods used to evaluate for next array.
    discard_prob: threshold on CDF to throw away an entire bin.  Should be very small
    """
    
    w = np.exp(lkl - np.max(lkl))
    npoints = len(w)
    sumw = np.sum(w)
    prob = w/sumw
    idx = np.argsort(prob)
    ecdf = np.cumsum(prob[idx])
    F = np.linspace(np.min(ecdf), 1., npoints)
    prob_stop_thr = lkl[idx][ecdf >= discard_prob][0]
    
    lkl_stop_thr = np.flip(np.sort(lkl))
    if len(lkl_stop_thr)>nsel:
        lkl_stop_thr = lkl_stop_thr[nsel]
    else:
        lkl_stop_thr = lkl_stop_thr[-1]
    lkl_thr = min(lkl_stop_thr, prob_stop_thr)

    truncp = np.sum(w[lkl < lkl_thr]) / sumw
            
    return lkl_thr, truncp

def sample_from_bins(xrange, dx, bu, ninbin):
    
        ndim = xrange.shape[0]
        xlo, xhi = xrange.T[0] + dx * bu, xrange.T[0] + dx * (bu+1)
        x = np.vstack([np.random.uniform(xlo[kk], xhi[kk], size = (npb, ndim)) for kk, npb in enumerate(ninbin)])
        return x


class MCSampler(object):
    """
    Class to define a set of parameter names, limits, and probability densities.
    """

    @staticmethod
    def match_params_from_args(args, params):
        """
        Given two unordered sets of parameters, one a set of all "basic" elements (strings) possible, and one a set of elements both "basic" strings and "combined" (basic strings in tuples), determine whether the sets are equivalent if no basic element is repeated.
        e.g. set A ?= set B
        ("a", "b", "c") ?= ("a", "b", "c") ==> True
        (("a", "b", "c")) ?= ("a", "b", "c") ==> True
        (("a", "b"), "d")) ?= ("a", "b", "c") ==> False  # basic element 'd' not in set B
        (("a", "b"), "d")) ?= ("a", "b", "d", "c") ==> False  # not all elements in set B represented in set A
        """
        not_common = set(args) ^ set(params)
        if len(not_common) == 0:
            # All params match
            return True
        if all([not isinstance(i, tuple) for i in not_common]):
            # The only way this is possible is if there are
            # no extraneous params in args
            return False

        to_match, against = [i for i in not_common if not isinstance(i, tuple)], [i for i in not_common if isinstance(i, tuple)]

        matched = []
        import itertools
        for i in range(2, max(list(map(len, against)))+1):
            matched.extend([t for t in itertools.permutations(to_match, i) if t in against])
        return (set(matched) ^ set(against)) == set()


    def __init__(self,n_chunk=400000):
        # Total number of samples drawn
        self.ntotal = 0
        # Parameter names
        self.params = set()
        self.params_ordered = []  # keep them in order. Important to break likelihood function need for names
        # If the pdfs aren't normalized, this will hold the normalization 
        # Cache for the sampling points
        self._rvs = {}
        # parameter -> cdf^{-1} function object
        # params for left and right limits
        self.llim, self.rlim = {}, {}


        self.n_chunk = n_chunk
        self.nbins = None
        self.ninbin = None

        self.pdf = {} # not used

        # MEASURES (=priors): ROS needs these at the sampler level, to clearly separate their effects
        # ASSUMES the user insures they are normalized
        self.prior_pdf = {}

        # histogram setup
        self.xpy = numpy
        self.identity_convert = lambda x: x  # if needed, convert to numpy format  (e.g, cupy.asnumpy)

    def setup(self):
        ndim = len(self.params)
        self.nbins = np.ones(ndim)
        self.binunique = np.array([ndim* [0]])
        self.ninbin   = [self.n_chunk]
        self.my_ranges =  np.array([[self.llim[x],self.rlim[x]] for x in self.params_ordered])
        self.dx = np.diff(self.my_ranges, axis = 1).flatten()  # weird way to code this
        self.dx0  = np.array(self.dx)   # Save initial prior widths (used for initial prior ragne at end/volume)
        self.cycle = 1


    def clear(self):
        """
        Clear out the parameters and their settings, as well as clear the sample cache.
        """
        self.params = set()
        self.params_ordered = []
        self.pdf = {}
        self._pdf_norm = defaultdict(lambda: 1.0)
        self._rvs = {}
        self.llim = {}
        self.rlim = {}
        self.adaptive = []


    def add_parameter(self, params, pdf,  cdf_inv=None, left_limit=None, right_limit=None, prior_pdf=None, adaptive_sampling=False):
        """
        Add one (or more) parameters to sample dimensions. params is either a string describing the parameter, or a tuple of strings. The tuple will indicate to the sampler that these parameters must be sampled together. left_limit and right_limit are on the infinite interval by default, but can and probably should be specified. If several params are given, left_limit, and right_limit must be a set of tuples with corresponding length. Sampling PDF is required, and if not provided, the cdf inverse function will be determined numerically from the sampling PDF.
        """
        self.params.add(params) # does NOT preserve order in which parameters are provided
        self.params_ordered.append(params)
        if rosDebugMessages: 
            print(" Adding parameter ", params, " with limits ", [left_limit, right_limit])
        if isinstance(params, tuple):
            assert all([lim[0] < lim[1] for lim in zip(left_limit, right_limit)])
            if left_limit is None:
                self.llim[params] = list(float("-inf"))*len(params)
            else:
                self.llim[params] = left_limit
            if right_limit is None:
                self.rlim[params] = list(float("+inf"))*len(params)
            else:
                self.rlim[params] = right_limit
        else:
            assert left_limit < right_limit
            if left_limit is None:
                self.llim[params] = float("-inf")
            else:
                self.llim[params] = left_limit
            if right_limit is None:
                self.rlim[params] = float("+inf")
            else:
                self.rlim[params] = right_limit
        self.pdf[params] = pdf
        self.prior_pdf[params] = prior_pdf

#        if adaptive_sampling:
#            print("   Adapting ", params)
#            self.adaptive.append(params)

    def prior_prod(self, x):
        """
        Evaluates prior_pdf(x), multiplying together all factors
        """
        p_out = np.ones(len(x))
        indx = 0
        for param in self.params_ordered:
            p_out *= self.prior_pdf[param](x[:,indx])
            indx +=1
        return p_out

    def draw_simple(self):
        # Draws
        x =  sample_from_bins(self.my_ranges, self.dx, self.binunique, self.ninbin)
        # probabilities at these points.  
        log_p = np.log(self.prior_prod(x))
        # Not including any sampling prior factors, since it is de facto uniform right now (just discarding 'irrelevant' regions)
        return x, log_p
        

    @profile
    def integrate_log(self, lnF, *args, xpy=xpy_default,**kwargs):
        """
        Integrate exp(lnF) returning lnI, by using n sample points, assuming integrand is lnF
        Does NOT allow for tuples of arguments, an unused feature in mcsampler

        tempering is done with lnF, suitably modified.

        kwargs:
        nmax -- total allowed number of sample points, will throw a warning if this number is reached before neff.
        neff -- Effective samples to collect before terminating. If not given, assume infinity
        n -- Number of samples to integrate in a 'chunk' -- default is 1000
        save_integrand -- Save the evaluated value of the integrand at the sample points with the sample point
        history_mult -- Number of chunks (of size n) to use in the adaptive histogramming: only useful if there are parameters with adaptation enabled
        tempering_exp -- Exponent to raise the weights of the 1-D marginalized histograms for adaptive sampling prior generation, by default it is 0 which will turn off adaptive sampling regardless of other settings
        temper_log -- Adapt in min(ln L, 10^(-5))^tempering_exp
        tempering_adapt -- Gradually evolve the tempering_exp based on previous history.
        floor_level -- *total probability* of a uniform distribution, averaged with the weighted sampled distribution, to generate a new sampled distribution
        n_adapt -- number of chunks over which to allow the pdf to adapt. Default is zero, which will turn off adaptive sampling regardless of other settings
        convergence_tests - dictionary of function pointers, each accepting self._rvs and self.params as arguments. CURRENTLY ONLY USED FOR REPORTING
        Pinning a value: By specifying a kwarg with the same of an existing parameter, it is possible to "pin" it. The sample draws will always be that value, and the sampling prior will use a delta function at that value.
        """


        xpy_here = self.xpy
        
        #
        # Determine stopping conditions
        #
        nmax = kwargs["nmax"] if "nmax" in kwargs else float("inf")
        neff = kwargs["neff"] if "neff" in kwargs else numpy.float128("inf")
        n = int(kwargs["n"] if "n" in kwargs else min(100000, nmax))
        convergence_tests = kwargs["convergence_tests"] if "convergence_tests" in kwargs else None
        save_no_samples = kwargs["save_no_samples"] if "save_no_samples" in kwargs else None


        #
        # Adaptive sampling parameters
        #
        n_history = int(kwargs["history_mult"]*n) if "history_mult" in kwargs else 2*n
        if n_history<=0:
            print("  Note: cannot adapt, no history ")

        tempering_exp = kwargs["tempering_exp"] if "tempering_exp" in kwargs else 0.0
        n_adapt = int(kwargs["n_adapt"]*n) if "n_adapt" in kwargs else 1000  # default to adapt to 1000 chunks, then freeze
        floor_integrated_probability = kwargs["floor_level"] if "floor_level" in kwargs else 0
        temper_log = kwargs["tempering_log"] if "tempering_log" in kwargs else False
        tempering_adapt = kwargs["tempering_adapt"] if "tempering_adapt" in kwargs else False
            

        save_intg = kwargs["save_intg"] if "save_intg" in kwargs else False
        # FIXME: The adaptive step relies on the _rvs cache, so this has to be
        # on in order to work
        if n_adapt > 0 and tempering_exp > 0.0:
            save_intg = True

        deltalnL = kwargs['igrand_threshold_deltalnL'] if 'igrand_threshold_deltalnL' in kwargs else float("Inf") # default is to return all
        deltaP    = kwargs["igrand_threshold_p"] if 'igrand_threshold_p' in kwargs else 0 # default is to omit 1e-7 of probability
        bFairdraw  = kwargs["igrand_fairdraw_samples"] if "igrand_fairdraw_samples" in kwargs else False
        n_extr = kwargs["igrand_fairdraw_samples_max"] if "igrand_fairdraw_samples_max" in kwargs else None

        bShowEvaluationLog = kwargs['verbose'] if 'verbose' in kwargs else False
        bShowEveryEvaluation = kwargs['extremely_verbose'] if 'extremely_verbose' in kwargs else False

        if bShowEvaluationLog:
            print(" .... mcsampler : providing verbose output ..... ")

        current_log_aggregate = None
        eff_samp = 0  # ratio of max weight to sum of weights
        maxlnL = -np.inf  # max lnL
        maxval=0   # max weight
        outvals=None  # define in top level scope
        self.ntotal = 0
        if bShowEvaluationLog:
            print("iteration Neff  sqrt(2*lnLmax) sqrt(2*lnLmarg) ln(Z/Lmax) int_var")

        self.n_chunk = n
        self.setup()  # sets up self.my_ranges, self.dx initially

        cycle =1

        # VT specific items
        loglkl_thr = -1e15
        enc_prob = 0.999 #The approximate upper limit on the final probability enclosed by histograms.
        V = 1  # nominal scale factor for hypercube volume
        ndim = len(self.params_ordered)
        allx, allloglkl, neffective = np.transpose([[]] * ndim), [], 0
        allp = []
        trunc_p = 1e-10 #How much probability analysis removes with evolution
        nsel = 1000# number of largest log-likelihood samples selected to estimate lkl_thr for the next cycle.

        ntotal_true = 0
        while (eff_samp < neff and ntotal_true < nmax ): #  and (not bConvergenceTests):
            # Draw samples. Note state variables binunique, ninbin -- so we can re-use the sampler later outside the loop
            rv, log_joint_p_prior = self.draw_simple()  # Beware reversed order of rv
            ntotal_true += len(rv)

            # Evaluate function, protecting argument order
            if 'no_protect_names' in kwargs:
                unpacked0 = rv.T
                lnL = lnF(*unpacked0)  # do not protect order
            else:
                unpacked = dict(list(zip(self.params_ordered,rv.T)))
                lnL= lnF(**unpacked)  # protect order using dictionary
            # take log if we are NOT using lnL
            if cupy_ok:
              if not(isinstance(lnL,cupy.ndarray)):
                lnL = identity_convert_togpu(lnL)  # send to GPU, if not already there


            # For now: no prior, just duplicate VT algorithm
            log_integrand =lnL  + log_joint_p_prior
#            log_weights = tempering_exp*lnL + log_joint_p_prior
            # log aggregate: NOT USED at present, remember the threshold is floating
            if current_log_aggregate is None:
              current_log_aggregate = init_log(log_integrand,xpy=xpy,special=xpy_special_default)
            else:
              current_log_aggregate = update_log(current_log_aggregate, log_integrand,xpy=xpy,special=xpy_special_default)
            
            loglkl = log_integrand # note we are putting the prior in here

            idxsel = np.where(loglkl > loglkl_thr)
            #only admit samples that lie inside the live volume, i.e. one that cross likelihood threshold
            allx = np.append(allx, rv[idxsel], axis = 0)
            allloglkl = np.append(allloglkl, loglkl[idxsel])
            allp = np.append(allp, log_joint_p_prior[idxsel])
            ninj = len(allloglkl)


            #just some test to verify if we dont discard more than 1 - Pthr probability
            at_final_threshold = np.round(enc_prob/trunc_p) - np.round(enc_prob/(1 - enc_prob)) == 0
            #Estimate likelihood threshold
            if not(at_final_threshold):
                loglkl_thr, truncp = get_likelihood_threshold(allloglkl, loglkl_thr, nsel, 1 - enc_prob - trunc_p)
                trunc_p += truncp
    
            # Select with threshold
            idxsel = np.where(allloglkl > loglkl_thr)
            allloglkl = allloglkl[idxsel]
            allp = allp[idxsel]
            allx = allx[idxsel]
            nrec = len(allloglkl)   # recovered size of active volume at present, after selection

            # Weights
            lw = allloglkl - np.max(allloglkl)
            w = np.exp(lw)
            neff_varaha = np.sum(w) ** 2 / np.sum(w ** 2)
            eff_samp = np.sum(w)/np.max(w)
 
            #New live volume based on new likelihood threshold
            V *= (nrec / ninj)
            delta_V = V / np.sqrt(nrec) 
 
            # Redefine bin sizes, reassign points to redefined hypercube set. [Asymptotically this becomes stationary]
            self.nbins = np.ones(ndim)*(1/delta_V) ** (1/ndim)  # uniform split in each dimension is normal, but we have array - can be irregular
            self.dx = np.diff(self.my_ranges, axis = 1).flatten() / self.nbins   # update bin widths
            binidx = ((allx - self.my_ranges.T[0]) / self.dx.T).astype(int) #bin indexs of the samples

            self.binunique = np.unique(binidx, axis = 0)
            self.ninbin = ((self.n_chunk // self.binunique.shape[0] + 1) * np.ones(self.binunique.shape[0])).astype(int)
            self.ntotal = current_log_aggregate[0]

            print(ntotal_true,eff_samp, np.round(neff_varaha), np.round(np.max(allloglkl), 1), len(allloglkl), np.mean(self.nbins), V,  len(self.binunique),  np.round(loglkl_thr, 1), trunc_p)
            cycle += 1
            if cycle > 1000:
                break

        # VT approach was to accumulate samples, but then prune them.  So we have all the lnL and x draws

        # write in variables requested in the standard format
        for indx in np.arange(len(self.params_ordered)):
            self._rvs[self.params_ordered[indx]] = allx[:,indx]  # pull out variable
        # write out log integrand
        self._rvs['log_integrand']  = allloglkl - allp
        self._rvs['log_joint_prior'] = allp
        self._rvs['log_joint_s_prior'] = np.ones(len(allloglkl))*(np.log(1/V) - np.sum(np.log(self.dx0)))  # effective uniform sampling on this volume

        # Manual estimate of integrand, done transparently (no 'log aggregate' or running calculation -- so memory hog
        log_wt = self._rvs["log_integrand"] + self._rvs["log_joint_prior"] - self._rvs["log_joint_s_prior"]
        log_int = xpy_special_default.logsumexp( log_wt) - np.log(len(log_wt))  # mean value
        rel_var = np.var( np.exp(log_wt - np.max(log_wt)))
        eff_samp = np.sum(np.exp(log_wt - np.max(log_wt)))
        maxval = np.max(allloglkl)  # max of log

        # Integral value: NOT RELIABLE b/c not just using samples in 
#        outvals = finalize_log(current_log_aggregate,xpy=xpy)
#        log_wt_tmp = allloglkl[np.isfinite(allloglkl)]  # remove infinite entries
#        outvals = init_log(log_wt_tmp)
#        print(outvals, log_int, maxval, current_log_aggregate)
#        eff_samp = xpy.exp(  outvals[0]+np.log(len(allloglkl)) - maxval)   # integral value minus floating point, which is maximum
#        rel_var = np.exp(outvals[1]/2  - outvals[0]  - np.log(self.ntotal)/2 )

        dict_return = {}
        return log_int, rel_var, eff_samp, dict_return

        # if outvals:
        #   out0 = outvals[0]; out1 = outvals[1]
        #   if not(isinstance(outvals[0], np.float64)):
        #     # type convert everything as needed
        #     out0 = identity_convert(out0)
        #   if not(isinstance(outvals[1], np.float64)):
        #     out1 = identity_convert(out1)
        #     eff_samp = identity_convert(eff_samp)
        #   return out0, out1 - np.log(self.ntotal), eff_samp, dict_return
        # else: # very strange case where we terminate early
        #   return None, None, None, None


    @profile
    def integrate(self, func, *args, **kwargs):
        """
        Integrate func, by using n sample points. Right now, all params defined must be passed to args must be provided, but this will change soon.
        Does NOT allow for tuples of arguments, an unused feature in mcsampler

        kwargs:
        nmax -- total allowed number of sample points, will throw a warning if this number is reached before neff.
        neff -- Effective samples to collect before terminating. If not given, assume infinity
        n -- Number of samples to integrate in a 'chunk' -- default is 1000
        save_integrand -- Save the evaluated value of the integrand at the sample points with the sample point
        history_mult -- Number of chunks (of size n) to use in the adaptive histogramming: only useful if there are parameters with adaptation enabled
        tempering_exp -- Exponent to raise the weights of the 1-D marginalized histograms for adaptive sampling prior generation, by default it is 0 which will turn off adaptive sampling regardless of other settings
        temper_log -- Adapt in min(ln L, 10^(-5))^tempering_exp
        tempering_adapt -- Gradually evolve the tempering_exp based on previous history.
        floor_level -- *total probability* of a uniform distribution, averaged with the weighted sampled distribution, to generate a new sampled distribution
        n_adapt -- number of chunks over which to allow the pdf to adapt. Default is zero, which will turn off adaptive sampling regardless of other settings
        convergence_tests - dictionary of function pointers, each accepting self._rvs and self.params as arguments. CURRENTLY ONLY USED FOR REPORTING
        Pinning a value: By specifying a kwarg with the same of an existing parameter, it is possible to "pin" it. The sample draws will always be that value, and the sampling prior will use a delta function at that value.
        """
        def ln_func(*args):
          return np.log(func(*args))
        infunc = ln_func
        use_lnL=False
        if 'use_lnL' in kwargs:   # should always be positive
          if kwargs['use_lnL']:
            infunc = func
            use_lnL=True
        log_int_val, log_var, eff_samp, dict_return =  self.integrate_log(func, **kwargs)  # pass it on, easier than mixed coding
        if use_lnL:
          self._rvs['integrand'] = self._rvs["log_integrand"]

        return log_int_val, log_var, eff_samp, dict_return