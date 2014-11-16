__author__ = 'arenduchintala'

from optparse import OptionParser
from math import exp, log, sqrt
from multiprocessing import Pool
import multiprocessing
from scipy.optimize import fmin_l_bfgs_b
import numpy as np
import FeatureEng as FE
import utils
from pprint import pprint
from collections import defaultdict
import random
import copy

global BOUNDARY_START, END_STATE, SPLIT, E_TYPE, T_TYPE, IBM_MODEL_1, HMM_MODEL
global cache_normalizing_decision, features_to_events, events_to_features, normalizing_decision_map
global trellis, max_jump_width, model_type, number_of_events, EPS, snippet, max_beam_width
global source, target, data_likelihood, event_grad
event_grad = {}
data_likelihood = 0.0
snippet = ''
EPS = 1e-8
IBM_MODEL_1 = "model1"
HMM_MODEL = "hmm"
max_jump_width = 10
max_beam_width = 20  # creates a span of +/- span centered around current token
trellis = []
cache_normalizing_decision = {}
BOUNDARY_START = "#START#"
BOUNDARY_END = "#END#"
NULL = "NULL"
E_TYPE = "EMISSION"
E_TYPE_PRE = "PREFIX_FEATURE"
E_TYPE_SUF = "SUFFIX_FEATURE"
T_TYPE = "TRANSITION"
ALL = "ALL_STATES"
fractional_counts = {}
number_of_events = 0
events_to_features = {}
features_to_events = {}
feature_index = {}
conditional_arc_index = {}
normalizing_decision_map = {}


def get_jump(aj, aj_1):
    if aj != NULL and aj_1 != NULL:
        jump = abs(aj_1 - aj)
        assert jump <= 2 * max_jump_width + 1
    else:
        jump = NULL
    return jump


def populate_features():
    global trellis, feature_index, source, target
    for treli_idx, treli in enumerate(trellis):
        for idx in treli:
            for t_idx, s_idx, L in treli[idx]:
                t_tok = target[treli_idx][t_idx]
                if s_idx == NULL:
                    s_tok = NULL
                else:
                    s_tok = source[treli_idx][s_idx]
                assert t_tok == t_tok
                assert s_tok == s_tok
                """
                emission features
                """
                ndm = normalizing_decision_map.get((E_TYPE, s_tok), set([]))
                ndm.add(t_tok)
                normalizing_decision_map[E_TYPE, s_tok] = ndm
                emission_context = s_tok
                emission_decision = t_tok
                emission_event = (E_TYPE, emission_decision, emission_context)
                ff_e = FE.get_wa_features_fired(type=E_TYPE, decision=emission_decision, context=emission_context)
                for f in ff_e:
                    feature_index[f] = len(feature_index) if f not in feature_index else feature_index[f]
                    ca2f = events_to_features.get(emission_event, set([]))
                    ca2f.add(f)
                    events_to_features[emission_event] = ca2f
                    f2ca = features_to_events.get(f, set([]))
                    f2ca.add(emission_event)
                    features_to_events[f] = f2ca

                if idx > 0 and model_type == HMM_MODEL:
                    for prev_t_idx, prev_s_idx, L in treli[idx - 1]:

                        prev_t_tok = target[treli_idx][prev_t_idx]
                        prev_s_tok = source[treli_idx][prev_s_idx] if prev_s_idx is not NULL else NULL
                        assert prev_t_tok == prev_t_tok
                        assert prev_s_tok == prev_s_tok
                        # an arc in an hmm can have the following information:
                        # prev_s_idx, prev_s_tok, s_idx, s_tok, t_idx, t_tok
                        prev_s = (prev_s_idx, prev_s_tok)
                        curr_s = (s_idx, s_tok)
                        curr_t = (t_idx, t_tok)
                        # arc = (prev_s, curr_s, curr_t)

                        # transition features

                        # jump = get_jump(s_idx, prev_s_idx)
                        transition_context = (prev_s_idx, L)
                        transition_decision = s_idx
                        transition_event = (T_TYPE, transition_decision, transition_context)
                        ff_t = FE.get_wa_features_fired(type=T_TYPE, decision=transition_decision,
                                                        context=transition_context)

                        ndm = normalizing_decision_map.get((T_TYPE, transition_context), set([]))
                        ndm.add(transition_decision)
                        normalizing_decision_map[T_TYPE, transition_context] = ndm
                        for f in ff_t:
                            feature_index[f] = len(feature_index) if f not in feature_index else feature_index[f]
                            ca2f = events_to_features.get(transition_event, set([]))
                            ca2f.add(f)
                            events_to_features[transition_event] = ca2f
                            f2ca = features_to_events.get(f, set([]))
                            f2ca.add(transition_event)
                            features_to_events[f] = f2ca


def get_decision_given_context(theta, type, decision, context):
    global normalizing_decision_map, cache_normalizing_decision, feature_index
    fired_features = FE.get_wa_features_fired(type=type, context=context, decision=decision)

    theta_dot_features = sum([theta[feature_index[f]] for f in fired_features])

    if (type, context) in cache_normalizing_decision:
        theta_dot_normalizing_features = cache_normalizing_decision[type, context]
    else:
        normalizing_decisions = normalizing_decision_map[type, context]
        theta_dot_normalizing_features = float('-inf')
        for d in normalizing_decisions:
            d_features = FE.get_wa_features_fired(type=type, context=context, decision=d)
            theta_dot_normalizing_features = utils.logadd(theta_dot_normalizing_features,
                                                          sum([theta[feature_index[f]] for f in d_features]))

        cache_normalizing_decision[type, context] = theta_dot_normalizing_features
    log_prob = round(theta_dot_features - theta_dot_normalizing_features, 10)
    if log_prob > 0.0:
        # print log_prob, type, decision, context
        # pdb.set_trace()
        log_prob = 0.0
        # raise Exception
    return log_prob


def get_backwards(theta, obs_id, alpha_pi, fc=None):
    global max_jump_width, trellis, source, target
    obs = trellis[obs_id]
    src = source[obs_id]
    tar = target[obs_id]
    n = len(obs) - 1  # index of last word
    end_state = obs[n][0]
    beta_pi = {(n, end_state): 0.0}
    S = alpha_pi[(n, end_state)]  # from line 13 in pseudo code
    fc = accumulate_fc(type=E_TYPE, alpha=0.0, beta=S, e=0.0, S=S, d=BOUNDARY_START, c=BOUNDARY_START, fc=fc)
    for k in range(n, 0, -1):
        for v in obs[k]:
            tk, aj, L = v
            t_tok = tar[tk]
            s_tok = src[aj] if aj is not NULL else NULL
            e = get_decision_given_context(theta, E_TYPE, decision=t_tok, context=s_tok)

            pb = beta_pi[(k, v)]
            fc = accumulate_fc(type=E_TYPE, alpha=alpha_pi[(k, v)], beta=beta_pi[k, v], e=e, S=S, d=t_tok, c=s_tok,
                               fc=fc)
            for u in obs[k - 1]:
                tk_1, aj_1, L = u
                t_tok_1 = tar[tk_1]
                s_tok_1 = src[aj_1] if aj_1 is not NULL else NULL
                context = (aj_1, L)
                if model_type == HMM_MODEL:
                    q = get_decision_given_context(theta, T_TYPE, decision=aj, context=context)
                    fc = accumulate_fc(type=T_TYPE, alpha=alpha_pi[k - 1, u], beta=beta_pi[k, v], q=q, e=e, d=aj,
                                       c=context,
                                       S=S, fc=fc)
                else:
                    q = log(1.0 / len(obs[k]))

                p = q + e
                beta_p = pb + p  # The beta includes the emission probability
                new_pi_key = (k - 1, u)
                if new_pi_key not in beta_pi:  # implements lines 16
                    beta_pi[new_pi_key] = beta_p
                else:
                    beta_pi[new_pi_key] = utils.logadd(beta_pi[new_pi_key], beta_p)
                    alpha_pi[(k - 1, u)] + p + beta_pi[(k, v)] - S
    if fc is None:
        return S, beta_pi
    else:
        return S, beta_pi, fc


def get_viterbi_and_forward(theta, obs_id):
    global max_jump_width, trellis, source, target
    src = source[obs_id]
    tar = target[obs_id]
    obs = trellis[obs_id]
    start_state = obs[0][0]
    pi = {(0, start_state): 0.0}
    alpha_pi = {(0, start_state): 0.0}
    arg_pi = {(0, start_state): []}
    for k in range(1, len(obs)):  # the words are numbered from 1 to n, 0 is special start character
        for v in obs[k]:  # [1]:
            max_prob_to_bt = {}
            sum_prob_to_bt = []
            for u in obs[k - 1]:  # [1]:
                tk, aj, L = v
                tk_1, aj_1, L = u
                t_tok = tar[tk]
                s_tok = src[aj] if aj is not NULL else NULL
                t_tok_1 = tar[tk_1]
                s_tok_1 = src[aj_1] if aj_1 is not NULL else NULL
                if model_type == HMM_MODEL:
                    context = (aj_1, L)
                    q = get_decision_given_context(theta, T_TYPE, decision=aj, context=context)
                else:
                    q = log(1.0 / len(obs[k]))

                e = get_decision_given_context(theta, E_TYPE, decision=t_tok, context=s_tok)

                p = pi[(k - 1, u)] + q + e
                alpha_p = alpha_pi[(k - 1, u)] + q + e
                if len(arg_pi[(k - 1, u)]) == 0:
                    bt = [u]
                else:
                    bt = [arg_pi[(k - 1, u)], u]
                max_prob_to_bt[p] = bt
                sum_prob_to_bt.append(alpha_p)

            max_bt = max_prob_to_bt[max(max_prob_to_bt)]
            new_pi_key = (k, v)
            pi[new_pi_key] = max(max_prob_to_bt)
            # print 'mu   ', new_pi_key, '=', pi[new_pi_key], exp(pi[new_pi_key])
            alpha_pi[new_pi_key] = utils.logadd_of_list(sum_prob_to_bt)
            # print 'alpha', new_pi_key, '=', alpha_pi[new_pi_key], exp(alpha_pi[new_pi_key])
            arg_pi[new_pi_key] = max_bt

    max_bt = max_prob_to_bt[max(max_prob_to_bt)]
    max_p = max(max_prob_to_bt)
    max_bt = utils.flatten_backpointers(max_bt)
    return max_bt, max_p, alpha_pi


def reset_fractional_counts():
    global fractional_counts, cache_normalizing_decision, number_of_events
    fractional_counts = {}  # dict((k, float('-inf')) for k in conditional_arc_index)
    cache_normalizing_decision = {}
    number_of_events = 0


def accumulate_fc(type, alpha, beta, d, S, c=None, k=None, q=None, e=None, fc=None):
    global fractional_counts
    if type == T_TYPE:
        update = alpha + q + e + beta - S
        if fc is None:
            fractional_counts[T_TYPE, d, c] = utils.logadd(update,
                                                           fractional_counts.get((T_TYPE, d, c,), float('-inf')))
        else:
            fc[T_TYPE, d, c] = utils.logadd(update, fc.get((T_TYPE, d, c,), float('-inf')))
    elif type == E_TYPE:
        update = alpha + beta - S  # the emission should be included in alpha
        if fc is None:
            fractional_counts[E_TYPE, d, c] = utils.logadd(update,
                                                           fractional_counts.get((E_TYPE, d, c,), float('-inf')))
        else:
            fc[E_TYPE, d, c] = utils.logadd(update, fc.get((E_TYPE, d, c,), float('-inf')))
    else:
        raise "Wrong type"
    if fc is not None:
        return fc


def write_probs(theta, save_probs):
    global feature_index
    write_probs = open(save_probs, 'w')
    write_probs.write(snippet)
    for fc in sorted(fractional_counts):
        (t, d, c) = fc
        prob = get_decision_given_context(theta, type=t, decision=d, context=c)
        str_t = reduce(lambda a, d: str(a) + '\t' + str(d), fc, '')
        write_probs.write(str_t.strip() + '\t' + str(round(prob, 5)) + '' + "\n")
    write_probs.flush()
    write_probs.close()
    print 'wrote probs to:', save_probs


def write_weights(theta, save_weights):
    global trellis, feature_index
    write_theta = open(save_weights, 'w')
    write_theta.write(snippet)
    for t in sorted(feature_index):
        str_t = reduce(lambda a, d: str(a) + '\t' + str(d), t, '')
        write_theta.write(str_t.strip() + '\t' + str(theta[feature_index[t]]) + '' + "\n")
    write_theta.flush()
    write_theta.close()
    print 'wrote weights to:', save_weights


def write_alignments(theta, save_align):
    global trellis, feature_index
    write_align = open(save_align, 'w')
    write_align.write(snippet)
    for idx, obs in enumerate(trellis[:]):
        max_bt, max_p, alpha_pi = get_viterbi_and_forward(theta, idx)
        w = ' '.join([str(tar_i - 1) + '-' + str(src_i - 1) for src_i, tar_i, L in max_bt if
                      (tar_i != NULL and tar_i > 0 and src_i > 0)])
        write_align.write(w + '\n')
    write_align.flush()
    write_align.close()
    print 'wrote alignments to:', save_align


def forward_backward(theta, idx):
    max_bt, max_p, alpha_pi = get_viterbi_and_forward(theta, idx)
    fc = {}
    S, beta_pi, fc = get_backwards(theta, idx, alpha_pi, fc=fc)
    return S, fc


def accumilate_data_likelihood(result):
    global data_likelihood, fractional_counts
    data_likelihood += result[0]
    fc = result[1]
    for k in fc:
        fractional_counts[k] = utils.logadd(fc[k], fractional_counts.get(k, float('-inf')))


def get_likelihood(theta, display=True, start_idx=None, end_idx=None):
    assert isinstance(theta, np.ndarray)
    global trellis, data_likelihood
    reset_fractional_counts()
    data_likelihood = 0.0
    st = 0 if start_idx is None else start_idx
    end = len(trellis) if end_idx is None else end_idx
    pool = Pool(processes=multiprocessing.cpu_count())  # uses all available CPUs
    for idx, obs in enumerate(trellis[st:end]):
        pool.apply_async(forward_backward, args=(theta, idx), callback=accumilate_data_likelihood)
    pool.close()
    pool.join()
    reg = np.sum(theta ** 2)
    ll = data_likelihood - (0.5 * reg)
    if display:
        print 'log likelihood:', ll
    return -ll


def get_likelihood_with_expected_counts(theta):
    global fractional_counts
    sum_likelihood = 0.0
    for event in fractional_counts:
        (t, d, c) = event
        A_dct = exp(fractional_counts[event])
        a_dct = get_decision_given_context(theta=theta, type=t, decision=d, context=c)
        sum_likelihood += A_dct * a_dct
    reg = np.sum(theta ** 2)
    return sum_likelihood - (0.5 * reg)


def event_mutliproc(event_j, theta):
    (t, dj, cj) = event_j
    a_dp_ct = exp(get_decision_given_context(theta, decision=dj, context=cj, type=t))
    sum_feature_j = 0.0
    norm_events = [(t, dp, cj) for dp in normalizing_decision_map[t, cj]]
    for event_i in norm_events:
        A_dct = exp(fractional_counts.get(event_i, 0.0))
        fj = 1.0 if event_i == event_j else 0.0
        sum_feature_j += A_dct * (fj - a_dp_ct)
    return event_j, sum_feature_j  # - abs(theta[event_j])  # this is the regularizing term


def accumilate_sum_feature_j(result):
    global event_grad
    (event_j, sum_feature_j) = result
    event_grad[event_j] = sum_feature_j


def get_gradient(theta):
    global fractional_counts, feature_index, event_grad
    event_grad = {}
    pool = Pool(processes=multiprocessing.cpu_count())
    for event_j in fractional_counts.keys():
        pool.apply_async(event_mutliproc, args=(event_j, theta), callback=accumilate_sum_feature_j)
    pool.close()
    pool.join()

    grad = -theta  # l2 regularization with lambda 0.5
    for e in event_grad:
        feats = events_to_features[e]
        for f in feats:
            grad[feature_index[f]] += event_grad[e]

    return -grad


def populate_trellis(source_corpus, target_corpus):
    global trellis, max_jump_width, max_beam_width
    for s_sent, t_sent in zip(source_corpus, target_corpus):
        t_sent.insert(0, BOUNDARY_START)
        s_sent.insert(0, BOUNDARY_START)

        t_sent.append(BOUNDARY_END)
        s_sent.append(BOUNDARY_END)

        fwd_trellis = {}
        for t_idx, t_tok in enumerate(t_sent):
            if t_tok == BOUNDARY_START:
                fwd_trellis[t_idx] = [(t_idx, 0, len(s_sent))]
            elif t_tok == BOUNDARY_END:
                fwd_trellis[t_idx] = [(t_idx, len(s_sent) - 1, len(s_sent))]
            else:
                max_prev = max([item[2] for item in fwd_trellis[t_idx - 1]])
                r = range(1,
                          (max_prev + max_jump_width + 1) if (max_prev + max_jump_width + 1) <= len(s_sent) - 1 else (
                              len(s_sent) - 1))
                s = [(t_idx, s_idx, len(s_sent)) for s_idx in r]
                fwd_trellis[t_idx] = s
        back_trellis = {}
        for t_idx, t_tok in reversed(list(enumerate(t_sent))):
            if t_tok == BOUNDARY_START:
                back_trellis[t_idx] = [(t_idx, t_idx, len(s_sent))]
            elif t_tok == BOUNDARY_END:
                back_trellis[t_idx] = [(t_idx, len(s_sent) - 1, len(s_sent))]
            else:
                min_prev = min([item[2] for item in back_trellis[t_idx + 1]])
                r = range(min_prev - max_jump_width if min_prev - max_jump_width >= 1 else 1, len(s_sent) - 1)
                s = [(t_idx, s_idx, len(s_sent)) for s_idx in r]
                back_trellis[t_idx] = s

        merged_trellis = {}
        for t_idx in back_trellis:
            s = list(set.intersection(set(fwd_trellis[t_idx]), set(back_trellis[t_idx])))
            if len(s) > max_beam_width:
                s_prime = sorted([(abs(item[2] - t_idx), item) for item in s])
                s = [item for dist, item in s_prime[:max_beam_width]]
            merged_trellis[t_idx] = s
            if t_idx == 0:
                pass
            elif t_idx == len(t_sent) - 1:
                pass
            else:
                merged_trellis[t_idx] += [(t_idx, NULL, len(s_sent))]

        trellis.append(merged_trellis)


def gradient_check_em():
    global EPS
    init_theta = initialize_theta(None)
    f_approx = {}
    for f in feature_index:
        theta_plus = copy.deepcopy(init_theta)
        theta_minus = copy.deepcopy(init_theta)
        theta_plus[feature_index[f]] = init_theta[feature_index[f]] + EPS
        get_likelihood(theta_plus)  # updates fractional counts
        val_plus = get_likelihood_with_expected_counts(theta_plus)
        theta_minus[feature_index[f]] = init_theta[feature_index[f]] - EPS
        get_likelihood(theta_minus)  # updates fractional counts
        val_minus = get_likelihood_with_expected_counts(theta_minus)
        f_approx[f] = (val_plus - val_minus) / (2 * EPS)

    my_grad = -get_gradient(init_theta)
    diff = []
    for k in sorted(f_approx):
        diff.append(abs(my_grad[feature_index[k]] - f_approx[k]))
        print str(round(my_grad[feature_index[k]] - f_approx[k], 3)).center(10), str(
            round(my_grad[feature_index[k]], 5)).center(10), \
            str(round(f_approx[k], 5)).center(10), k
    f_approx = sorted([(feature_index[k], v) for k, v in f_approx.items()])
    f_approx = np.array([v for k, v in f_approx])

    print 'component difference:', round(sum(diff), 3), \
        'cosine similarity:', utils.cosine_sim(f_approx, my_grad), \
        ' sign difference', utils.sign_difference(f_approx, my_grad)


def gradient_check_lbfgs():
    global EPS, feature_index
    init_theta = initialize_theta(None)
    chk_grad = utils.gradient_checking(init_theta, EPS, get_likelihood)
    my_grad = get_gradient(init_theta)
    diff = []
    for k in xrange(len(chk_grad)):
        diff.append(abs(my_grad[k] - chk_grad[k]))
        print str(round(my_grad[k] - chk_grad[k], 5)).center(10), str(
            round(my_grad[k], 5)).center(10), \
            str(round(chk_grad[k], 5)).center(10), k

    print 'component difference:', round(sum(diff), 3), \
        'cosine similarity:', utils.cosine_sim(chk_grad, my_grad), \
        ' sign difference', utils.sign_difference(chk_grad, my_grad)


def initialize_theta(input_weights):
    global feature_index
    init_theta = np.random.uniform(-1.0, 1.0, len(feature_index))
    if input_weights is not None:
        print 'reading initial weights...'
        for l in open(options.input_weights, 'r').readlines():
            l_key = tuple(l.split()[:-1])
            if l_key in feature_index:
                init_theta[feature_index[l_key]] = float(l.split()[-1:][0])
                # print 'updated ', l_key
            else:
                # print 'ignored', l_key
                pass
    else:
        print 'no initial weights given, random initial weights assigned...'
    return init_theta


if __name__ == "__main__":
    trellis = []

    opt = OptionParser()

    opt.add_option("-t", dest="target_corpus", default="data/small/en-toy")
    opt.add_option("-s", dest="source_corpus", default="data/small/fr-toy")
    opt.add_option("--iw", dest="input_weights", default=None)
    opt.add_option("--ow", dest="output_weights", default="theta", help="extention of trained weights file")
    opt.add_option("--oa", dest="output_alignments", default="alignments", help="extension of alignments files")
    opt.add_option("--op", dest="output_probs", default="probs", help="extension of probabilities")
    opt.add_option("-g", dest="test_gradient", default="false")
    opt.add_option("-a", dest="algorithm", default="LBFGS",
                   help="use 'EM' 'LBFGS' 'SGD'")
    opt.add_option("-m", dest="model", default=IBM_MODEL_1, help="'model1' or 'hmm'")
    (options, _) = opt.parse_args()

    model_type = options.model
    source = [s.strip().split() for s in open(options.source_corpus, 'r').readlines() if s.strip() != '']
    target = [s.strip().split() for s in open(options.target_corpus, 'r').readlines() if s.strip() != '']
    populate_trellis(source, target)
    populate_features()

    snippet = "#" + str(opt.values) + "\n"
    print snippet

    if options.algorithm == "LBFGS":
        if options.test_gradient.lower() == "true":
            gradient_check_lbfgs()
        else:
            print 'skipping gradient check...'
            init_theta = initialize_theta(options.input_weights)
            (theta, fopt, return_status) = fmin_l_bfgs_b(get_likelihood, init_theta, get_gradient, pgtol=0.1)
            write_alignments(theta, options.algorithm + '.' + model_type + '.' + options.output_alignments)
            write_weights(theta, options.algorithm + '.' + model_type + '.' + options.output_weights)
            write_probs(theta, options.algorithm + '.' + model_type + '.' + options.output_probs)

    if options.algorithm == "EM":
        if options.test_gradient.lower() == "true":
            gradient_check_em()
        else:
            print 'skipping gradient check...'
            init_theta = initialize_theta(options.input_weights)
            new_e = get_likelihood(init_theta)
            old_e = float('-inf')
            converged = False
            while not converged:
                (theta, fopt, return_status) = fmin_l_bfgs_b(get_likelihood_with_expected_counts, init_theta,
                                                             get_gradient,
                                                             pgtol=0.1,
                                                             maxfun=5)
                new_e = get_likelihood(theta)  # this will also update expected counts
                converged = round(abs(old_e - new_e), 2) == 0.0
                old_e = new_e
            write_alignments(theta, options.algorithm + '.' + model_type + '.' + options.output_alignments)
            write_weights(theta, options.algorithm + '.' + model_type + '.' + options.output_weights)
            write_probs(theta, options.algorithm + '.' + model_type + '.' + options.output_probs)

    if options.algorithm == "SGD":
        theta = initialize_theta(options.input_weights)
        get_likelihood(theta)
        reset_fractional_counts()
        eta0 = 0.5
        I = 1.0
        step_size = np.zeros(np.shape(theta))  # dict((k, 0.0) for k in feature_index)
        ft = np.zeros(np.shape(theta))  # dict((k, 0.0) for k in feature_index)
        idxs = range(len(trellis))
        t = 0
        for iter in xrange(5):
            random.shuffle(idxs)
            for i in idxs:
                t += 1

                new_ll = get_likelihood(theta, display=False, start_idx=idxs[i], end_idx=idxs[i] + 1)
                grad = get_gradient(theta)
                ft += grad ** 2
                tmp = eta0 * grad
                ft_I = I + ft
                theta = tmp / ft_I

            print 'full ll', get_likelihood(theta, display=False)
        write_alignments(theta, options.algorithm + '.' + model_type + '.' + options.output_alignments)
        write_weights(theta, options.algorithm + '.' + model_type + '.' + options.output_weights)
        write_probs(theta, options.algorithm + '.' + model_type + '.' + options.output_probs)