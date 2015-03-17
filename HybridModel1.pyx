__author__ = 'arenduchintala'

from math import log, exp
import utils

import numpy as np  #THIS SHOULD BE HERE.. PYCHARM NOT SMART ENOUGH TO KNOW CYTHON NEEDS IT

cimport numpy as np
from const import E_TYPE, HYBRID_MODEL_1

from const import NULL as _NULL_
from cyth.cyth_common import populate_trellis, load_model1_probs, load_dictionary_features, populate_features, \
    get_source_to_target_firing, pre_compute_ets, load_corpus_file, get_wa_features_fired, write_probs, write_weights, \
    write_alignments, write_alignments_col, write_alignments_col_tok


cdef class HybridModel1(object):
    cdef public np.ndarray du_count
    cdef public double rc
    cdef int max_beam_width, max_jump_width
    cdef public list target, source, trellis, eindex
    cdef set target_types, source_types
    cdef public dict  dictionary_features, findex
    cdef dict model1_probs, e2f, f2e, fcounts, ecounts, e2eindex
    cdef dict normalizing_decision_map
    cdef dict s2t_firing, ets, cache_normalizing_decision

    def __init__(self, char *source_corpus_file,
                 char *target_corpus_file,
                 char *model1_probs_file,
                 double rc,
                 char *dictionary_feature_file):

        self.rc = rc
        self.target, self.target_types = load_corpus_file(target_corpus_file)
        self.source, self.source_types = load_corpus_file(source_corpus_file)
        self.source_types.add(_NULL_)
        self.max_jump_width = 10
        self.max_beam_width = 20
        self.trellis = populate_trellis(self.source, self.target, self.max_jump_width, self.max_beam_width)
        self.dictionary_features = load_dictionary_features(dictionary_feature_file)
        self.model1_probs = load_model1_probs(model1_probs_file)

        self.e2f, self.f2e, self.findex, self.fcounts, self.eindex, self.e2eindex, self.ecounts, self.normalizing_decision_map, self.du_count = populate_features(
            trellis=self.trellis,
            source=self.source,
            target=self.target,
            model_type=HYBRID_MODEL_1,
            dictionary_features=self.dictionary_features)
        print 'number of features used', len(self.findex)
        self.s2t_firing = get_source_to_target_firing(self.e2f)
        self.ets = pre_compute_ets(self.model1_probs, self.s2t_firing, self.target_types, self.source_types)
        self.cache_normalizing_decision = {}
    def get_decision_given_context(self, theta, type, decision, context):
        return self._get_decision_given_context(theta, type, decision, context)

    cdef _get_decision_given_context(self, np.ndarray theta, char *type, char *decision, char *context):
        m1_event_prob = self.model1_probs.get((decision, context), 0.0)
        fired_features = get_wa_features_fired(type=type, decision=decision, context=context,
                                               dictionary_features=self.dictionary_features, hybrid=True)
        theta_dot_features = sum([theta[self.findex[f]] * f_wt for f_wt, f in fired_features])
        numerator = m1_event_prob * exp(theta_dot_features)
        if (type, context) in self.cache_normalizing_decision:
            denom = self.cache_normalizing_decision[type, context]
        else:
            denom = self.ets[context]
            target_firings = self.s2t_firing.get(context, set([]))
            for tf in target_firings:
                m1_tf_event_prob = self.model1_probs.get((tf, context), 0.0)
                tf_fired_features = get_wa_features_fired(type=type, decision=tf, context=context,
                                                          dictionary_features=self.dictionary_features, hybrid=True)
                tf_theta_dot_features = sum([theta[self.findex[f]] * f_wt for f_wt, f in tf_fired_features])
                denom += m1_tf_event_prob * exp(tf_theta_dot_features)
            self.cache_normalizing_decision[type, context] = denom
        try:
            log_prob = log(numerator) - log(denom)
        except ValueError:
            print numerator, denom, decision, context, m1_event_prob, theta_dot_features
            raise BaseException
        return log_prob

    cdef reset_fractional_counts(self):
        self.fcounts = {}
        self.cache_normalizing_decision = {}
        return True

    cdef get_model1_forward(self, theta, int obs_id):
        obs = self.trellis[obs_id]
        max_bt = [-1] * len(obs)
        p_st = 0.0
        for t_idx in obs:
            t_tok = self.target[obs_id][t_idx]
            sum_e = float('-inf')
            max_e = float('-inf')
            max_s_idx = None
            sum_sj = float('-inf')
            for _, s_idx in obs[t_idx]:
                s_tok = self.source[obs_id][s_idx] if s_idx is not _NULL_ else _NULL_
                e = self._get_decision_given_context(theta, E_TYPE, decision=t_tok, context=s_tok)
                sum_e = utils.logadd(sum_e, e)
                q = log(1.0 / len(obs[t_idx]))
                sum_sj = utils.logadd(sum_sj, e + q)
                if e > max_e:
                    max_e = e
                    max_s_idx = s_idx
            max_bt[t_idx] = (t_idx, max_s_idx)
            p_st += sum_sj

            # update fractional counts
            for _, s_idx in obs[t_idx]:
                s_tok = self.source[obs_id][s_idx] if s_idx is not _NULL_ else _NULL_
                e = self._get_decision_given_context(theta, E_TYPE, decision=t_tok, context=s_tok)
                delta = e - sum_e
                event = (E_TYPE, t_tok, s_tok)
                self.fcounts[event] = utils.logadd(delta, self.fcounts.get(event, float('-inf')))

        return max_bt[:-1], p_st

    def get_likelihood(self, theta):
        return self._get_likelihood(theta)

    cdef  double _get_likelihood(self, theta):
        assert isinstance(theta, np.ndarray)
        assert len(theta) == len(self.findex)
        self.reset_fractional_counts()
        data_likelihood = 0.0
        batch = range(0, len(self.trellis))
        for idx in batch:
            max_bt, S = self.get_model1_forward(theta, idx)
            data_likelihood += S
        reg = np.sum(theta ** 2)
        ll = data_likelihood - (self.rc * reg)
        e1 = self._get_decision_given_context(theta, E_TYPE, decision='.', context=_NULL_)
        e2 = self._get_decision_given_context(theta, E_TYPE, decision='.', context='.')
        print 'log likelihood:', ll, 'p(.|NULL)', e1, 'p(.|.)', e2
        return -ll

    def get_gradient(self, theta):
        return self._get_gradient(theta)

    cdef  np.ndarray _get_gradient(self, theta):
        assert len(theta) == len(self.findex)
        event_grad = {}
        for event_j in self.e2f:
            (t, dj, cj) = event_j
            f_val, f = \
                get_wa_features_fired(type=t, context=cj, decision=dj, dictionary_features=self.dictionary_features,
                                      hybrid=True)[0]
            a_dp_ct = exp(self._get_decision_given_context(theta, decision=dj, context=cj, type=t)) * f_val
            sum_feature_j = 0.0
            norm_events = [(t, dp, cj) for dp in self.normalizing_decision_map[t, cj]]
            for event_i in norm_events:
                A_dct = exp(self.fcounts.get(event_i, 0.0))
                if event_i == event_j:
                    (ti, di, ci) = event_i
                    fj, f = \
                        get_wa_features_fired(type=ti, context=ci, decision=di,
                                              dictionary_features=self.dictionary_features,
                                              hybrid=True)[0]
                else:
                    fj = 0.0
                sum_feature_j += A_dct * (fj - a_dp_ct)
            event_grad[event_j] = sum_feature_j  # - abs(theta[event_j])  # this is the regularizing term


        # grad = np.zeros_like(theta)
        grad = -2 * self.rc * theta  # l2 regularization with lambda 0.5
        for e in event_grad:
            feats = self.e2f.get(e, [])
            for f in feats:
                grad[self.findex[f]] += event_grad[e]

        # for s in seen_index:
        # grad[s] += -theta[s]  # l2 regularization with lambda 0.5
        assert len(grad) == len(self.findex)
        return -grad

    def get_best_seq(self, theta, obs_id):
        return self._get_best_seq(theta, obs_id)

    cdef _get_best_seq(self, np.ndarray theta, int obs_id):
        obs = self.trellis[obs_id]
        max_bt = [-1] * len(obs)
        p_st = 0.0
        for t_idx in obs:
            t_tok = self.target[obs_id][t_idx]
            sum_e = float('-inf')
            max_e = float('-inf')
            max_s_idx = None
            sum_sj = float('-inf')
            for _, s_idx in obs[t_idx]:
                s_tok = self.source[obs_id][s_idx] if s_idx is not _NULL_ else _NULL_
                e = self._get_decision_given_context(theta, E_TYPE, decision=t_tok, context=s_tok)
                sum_e = utils.logadd(sum_e, e)
                q = log(1.0 / len(obs[t_idx]))
                sum_sj = utils.logadd(sum_sj, e + q)
                if e > max_e:
                    max_e = e
                    max_s_idx = s_idx
            max_bt[t_idx] = (t_idx, max_s_idx)
            p_st += sum_sj
        return max_bt[:-1], p_st

    def write_logs(self, theta, out_weights_file, out_probs_file, out_alignments):
        return self._write_logs(theta, out_weights_file, out_probs_file, out_alignments)

    cdef  _write_logs(self, theta, char *out_weights_file, char *out_probs_file, char *out_alignments):
        feature_val_typ = 'bin'
        name_prefix = '.'.join(['sp', 'LBFGS', str(self.rc), HYBRID_MODEL_1, feature_val_typ])
        write_weights(theta, name_prefix + '.' + out_weights_file, self.findex)
        write_probs(theta, name_prefix + '.' + out_probs_file, self.fcounts, self.get_decision_given_context)

        write_alignments(theta, name_prefix + '.' + out_alignments, self.trellis, self.get_best_seq)
        write_alignments_col(theta, name_prefix + '.' + out_alignments, self.trellis, self.get_best_seq)
        write_alignments_col_tok(theta, name_prefix + '.' + out_alignments, self.trellis, self.source,
                                 self.target, self.get_best_seq)
        return True

    def train(self, theta, threshold, maxiter):
        return self._train(theta, threshold, maxiter)

    cdef _train(self, theta, double threshold, int maxiter):
        #t1 = minimize(self._get_likelihood, theta, method='L-BFGS-B', jac=self._get_gradient, tol=threshold,
        #              options={'maxiter': maxiter})
        #theta = t1.x
        return theta