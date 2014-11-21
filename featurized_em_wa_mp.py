__author__ = 'arenduchintala'

from optparse import OptionParser
from math import exp, log
import sys
import multiprocessing
from multiprocessing import Pool
from scipy.optimize import minimize
import numpy as np
import FeatureEng as FE
import utils
import random
import copy
import pdb
from pprint import pprint

global BOUNDARY_START, END_STATE, SPLIT, E_TYPE, T_TYPE, IBM_MODEL_1, HMM_MODEL
global cache_normalizing_decision, features_to_events, events_to_features, normalizing_decision_map
global trellis, max_jump_width, model_type, number_of_events, EPS, snippet, max_beam_width, rc
global source, target, data_likelihood, event_grad, feature_index, event_index, itercount, itermediate_log
itercount = 0
event_grad = {}
data_likelihood = 0.0
snippet = ''
EPS = 1e-5
rc = 0.25
itermediate_log = 0
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
event_index = []
conditional_arc_index = {}
normalizing_decision_map = {}


def populate_features():
    global trellis, feature_index, source, target, event_index
    event_index = set([])
    for treli_idx, treli in enumerate(trellis):
        for idx in treli:
            for t_idx, s_idx in treli[idx]:
                t_tok = target[treli_idx][t_idx]
                if s_idx == NULL:
                    s_tok = NULL
                else:
                    s_tok = source[treli_idx][s_idx]
                """
                emission features
                """
                ndm = normalizing_decision_map.get((E_TYPE, s_tok), set([]))
                ndm.add(t_tok)
                normalizing_decision_map[E_TYPE, s_tok] = ndm
                emission_context = s_tok
                emission_decision = t_tok
                emission_event = (E_TYPE, emission_decision, emission_context)
                event_index.add(emission_event)
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
                    for prev_t_idx, prev_s_idx in treli[idx - 1]:
                        """
                        transition features
                        """
                        transition_context = prev_s_idx
                        transition_decision = s_idx
                        transition_event = (T_TYPE, transition_decision, transition_context)
                        event_index.add(transition_event)
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
    event_index = sorted(list(event_index))


def get_decision_given_context(theta, type, decision, context):
    global normalizing_decision_map, cache_normalizing_decision, feature_index
    fired_features = FE.get_wa_features_fired(type=type, context=context, decision=decision)

    theta_dot_features = sum([theta[feature_index[f]] for f in fired_features])

    if (type, context) in cache_normalizing_decision:
        theta_dot_normalizing_features = cache_normalizing_decision[type, context]
    else:
        normalizing_decisions = normalizing_decision_map[type, context]
        theta_dot_normalizing_features = 0
        for d in normalizing_decisions:
            d_features = FE.get_wa_features_fired(type=type, context=context, decision=d)
            theta_dot_normalizing_features += exp(sum([theta[feature_index[f]] for f in d_features]))

        theta_dot_normalizing_features = log(theta_dot_normalizing_features)
        cache_normalizing_decision[type, context] = theta_dot_normalizing_features
    log_prob = round(theta_dot_features - theta_dot_normalizing_features, 10)
    if log_prob > 0.0:
        log_prob = 0.0  # this happens if we truncate the LBFGS alg with maxiter
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
            tk, aj = v
            t_tok = tar[tk]
            s_tok = src[aj] if aj is not NULL else NULL
            e = get_decision_given_context(theta, E_TYPE, decision=t_tok, context=s_tok)

            pb = beta_pi[(k, v)]
            fc = accumulate_fc(type=E_TYPE, alpha=alpha_pi[(k, v)], beta=beta_pi[k, v], e=e, S=S, d=t_tok, c=s_tok,
                               fc=fc)
            for u in obs[k - 1]:
                tk_1, aj_1 = u
                t_tok_1 = tar[tk_1]
                s_tok_1 = src[aj_1] if aj_1 is not NULL else NULL
                context = aj_1
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
                tk, aj = v
                tk_1, aj_1 = u
                t_tok = tar[tk]
                s_tok = src[aj] if aj is not NULL else NULL
                t_tok_1 = tar[tk_1]
                s_tok_1 = src[aj_1] if aj_1 is not NULL else NULL
                if model_type == HMM_MODEL:
                    context = aj_1
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
    if type == T_TYPE:
        update = alpha + q + e + beta - S
        fc[T_TYPE, d, c] = utils.logadd(update, fc.get((T_TYPE, d, c,), float('-inf')))
    elif type == E_TYPE:
        update = alpha + beta - S  # the emission should be included in alpha
        fc[E_TYPE, d, c] = utils.logadd(update, fc.get((E_TYPE, d, c,), float('-inf')))
    else:
        raise "Wrong type"
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


def write_alignments_col_tok(theta, save_align):
    save_align += '.col.tokens'
    global trellis, feature_index, source, target
    write_align = open(save_align, 'w')
    # write_align.write(snippet)
    for idx, obs in enumerate(trellis[:]):
        max_bt, max_p, alpha_pi = get_viterbi_and_forward(theta, idx)
        for tar_i, src_i in max_bt:
            if src_i != NULL and src_i > 0 and tar_i > 0:
                write_align.write(str(idx + 1) + ' ' + source[idx][src_i] + ' ' + target[idx][tar_i] + '\n')
    write_align.flush()
    write_align.close()
    print 'wrote alignments to:', save_align


def write_alignments_col(theta, save_align):
    save_align += '.col'
    global trellis, feature_index
    write_align = open(save_align, 'w')
    # write_align.write(snippet)
    for idx, obs in enumerate(trellis[:]):
        max_bt, max_p, alpha_pi = get_viterbi_and_forward(theta, idx)
        for tar_i, src_i in max_bt:
            if src_i != NULL and tar_i > 0 and src_i > 0:
                write_align.write(str(idx + 1) + ' ' + str(src_i) + ' ' + str(tar_i) + '\n')
    write_align.flush()
    write_align.close()
    print 'wrote alignments to:', save_align


def write_alignments(theta, save_align):
    global trellis, feature_index
    write_align = open(save_align, 'w')
    # write_align.write(snippet)
    for idx, obs in enumerate(trellis[:]):
        max_bt, max_p, alpha_pi = get_viterbi_and_forward(theta, idx)
        w = ' '.join(
            [str(src_i) + '-' + str(tar_i) for tar_i, src_i in max_bt if src_i != NULL and tar_i > 0 and src_i > 0])
        write_align.write(w + '\n')
    write_align.flush()
    write_align.close()
    print 'wrote alignments to:', save_align


def batch_likelihood(theta, batch):
    dl = 0.0
    batch_fc = {}
    for idx in batch:
        max_bt, max_p, alpha_pi = get_viterbi_and_forward(theta, idx)
        S, beta_pi, batch_fc = get_backwards(theta, idx, alpha_pi, batch_fc)
        dl += S
    return dl, batch_fc


def batch_accumilate_likelihood(result):
    global data_likelihood, fractional_counts
    data_likelihood += result[0]
    fc = result[1]
    for k in fc:
        fractional_counts[k] = utils.logadd(fc[k], fractional_counts.get(k, float('-inf')))


def get_likelihood(theta, display=True):
    assert isinstance(theta, np.ndarray)
    assert len(theta) == len(feature_index)
    global trellis, data_likelihood, rc, itercount
    reset_fractional_counts()
    data_likelihood = 0.0
    cpu_count = multiprocessing.cpu_count()
    pool = Pool(processes=cpu_count)  # uses all available CPUs
    full = range(0, len(trellis))
    batches = np.array_split(full, cpu_count)
    for batch in batches:
        pool.apply_async(batch_likelihood, args=(theta, batch), callback=batch_accumilate_likelihood)
    pool.close()
    pool.join()
    reg = np.sum(theta ** 2)
    ll = data_likelihood - (rc * reg)
    if display:
        print itercount, 'log likelihood:', ll
    itercount += 1
    if itermediate_log > 0 and itercount % itermediate_log == 0:
        write_logs(theta, itercount)
    return -ll


def batch_gradient(theta, batch_fractional_counts):
    global event_index
    eg = {}
    for idx in batch_fractional_counts:
        (t, dj, cj) = event_index[idx]
        a_dp_ct = exp(get_decision_given_context(theta, decision=dj, context=cj, type=t))
        sum_feature_j = 0.0
        norm_events = [(t, dp, cj) for dp in normalizing_decision_map[t, cj]]
        for event_i in norm_events:
            A_dct = exp(fractional_counts.get(event_i, 0.0))
            fj = 1.0 if event_i == (t, dj, cj) else 0.0
            sum_feature_j += A_dct * (fj - a_dp_ct)
        eg[(t, dj, cj)] = sum_feature_j
    return eg


def batch_accumilate_gradient(result):
    global event_grad
    for event_j in result:
        if event_j in event_grad:
            raise 'should this happen?'
        else:
            event_grad[event_j] = result[event_j]


def get_gradient(theta):
    global fractional_counts, event_index, feature_index, event_grad, rc
    assert len(theta) == len(feature_index)
    event_grad = {}
    cpu_count = multiprocessing.cpu_count()
    pool = Pool(processes=cpu_count)  # uses all available CPUs
    batches_fractional_counts = np.array_split(range(len(event_index)), cpu_count)
    for batch_of_fc in batches_fractional_counts:
        pool.apply_async(batch_gradient, args=(theta, batch_of_fc), callback=batch_accumilate_gradient)
    pool.close()
    pool.join()
    # grad = np.zeros_like(theta)
    grad = -2 * rc * theta  # l2 regularization with lambda 0.5
    for e in event_grad:
        feats = events_to_features[e]
        for f in feats:
            grad[feature_index[f]] += event_grad[e]

    # for s in seen_index:
    # grad[s] += -theta[s]  # l2 regularization with lambda 0.5
    assert len(grad) == len(feature_index)
    return -grad


def get_likelihood_with_expected_counts(theta, batch=None, display=False):
    global fractional_counts
    sum_likelihood = 0.0
    for event in fractional_counts:
        (t, d, c) = event
        A_dct = exp(fractional_counts[event])
        a_dct = get_decision_given_context(theta=theta, type=t, decision=d, context=c)
        sum_likelihood += A_dct * a_dct
    reg = np.sum(theta ** 2)
    sum_likelihood -= (rc * reg)
    if display:
        print '\tec log likelihood:', sum_likelihood
    return -sum_likelihood


def populate_trellis(source_corpus, target_corpus):
    global max_jump_width, max_beam_width
    new_trellis = []
    for s_sent, t_sent in zip(source_corpus, target_corpus):
        t_sent.insert(0, BOUNDARY_START)
        t_sent.append(BOUNDARY_END)
        s_sent.insert(0, BOUNDARY_START)
        s_sent.append(BOUNDARY_END)
        trelli = {}
        for t_idx, t_tok in enumerate(t_sent):
            if t_idx == 0:
                state_options = [(t_idx, s_sent.index(BOUNDARY_START))]
            elif t_idx == len(t_sent) - 1:
                state_options = [(t_idx, s_sent.index(BOUNDARY_END))]
            else:
                state_options = [(t_idx, s_idx) for s_idx, s_tok in enumerate(s_sent) if
                                 s_tok != BOUNDARY_END and s_tok != BOUNDARY_START]
                state_options += [(t_idx, NULL)]
            trelli[t_idx] = state_options
        new_trellis.append(trelli)
    return new_trellis


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

    my_grad = get_gradient(init_theta)
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
    for f in sorted(feature_index):  # xrange(len(chk_grad)):
        k = feature_index[f]
        diff.append(abs(my_grad[k] - chk_grad[k]))
        print str(round(my_grad[k] - chk_grad[k], 5)).center(10), str(
            round(my_grad[k], 5)).center(10), \
            str(round(chk_grad[k], 5)).center(10), f

    print 'component difference:', round(sum(diff), 3), \
        'cosine similarity:', utils.cosine_sim(chk_grad, my_grad), \
        ' sign difference', utils.sign_difference(chk_grad, my_grad)


def initialize_theta(input_weights):
    global feature_index
    init_theta = np.random.uniform(1.0, 1.0, len(feature_index))
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


def write_logs(theta, current_iter):
    global trellis
    name_prefix = '.'.join(
        [options.algorithm, str(rc), model_type])
    if itermediate_log > 0:
        name_prefix += '.' + str(current_iter)
    write_weights(theta, name_prefix + '.' + options.output_weights)
    write_probs(theta, name_prefix + '.' + options.output_probs)

    if options.source_test is not None and options.target_test is not None:
        source = [s.strip().split() for s in open(options.source_test, 'r').readlines()]
        target = [t.strip().split() for t in open(options.target_test, 'r').readlines()]
        trellis = populate_trellis(source, target)

    write_alignments(theta, name_prefix + '.' + options.output_alignments)
    write_alignments_col(theta, name_prefix + '.' + options.output_alignments)
    write_alignments_col_tok(theta, name_prefix + '.' + options.output_alignments)


if __name__ == "__main__":
    trellis = []
    opt = OptionParser()
    opt.add_option("-t", dest="target_corpus", default="experiment/data/toy.fr")
    opt.add_option("-s", dest="source_corpus", default="experiment/data/toy.en")
    opt.add_option("--tt", dest="target_test", default="experiment/data/toy.fr")
    opt.add_option("--ts", dest="source_test", default="experiment/data/toy.en")
    opt.add_option("--il", dest="intermediate_log", default="0")
    opt.add_option("--iw", dest="input_weights", default=None)
    opt.add_option("--ow", dest="output_weights", default="theta", help="extention of trained weights file")
    opt.add_option("--oa", dest="output_alignments", default="alignments", help="extension of alignments files")
    opt.add_option("--op", dest="output_probs", default="probs", help="extension of probabilities")
    opt.add_option("-g", dest="test_gradient", default="false")
    opt.add_option("-r", dest="regularization_coeff", default="0.0")
    opt.add_option("-a", dest="algorithm", default="LBFGS",
                   help="use 'EM' 'LBFGS' 'SGD'")
    opt.add_option("-m", dest="model", default=IBM_MODEL_1, help="'model1' or 'hmm'")
    (options, _) = opt.parse_args()
    rc = float(options.regularization_coeff)
    itermediate_log = int(options.intermediate_log)
    model_type = options.model
    source = [s.strip().split() for s in open(options.source_corpus, 'r').readlines()]
    target = [s.strip().split() for s in open(options.target_corpus, 'r').readlines()]
    trellis = populate_trellis(source, target)
    populate_features()
    snippet = "#" + str(opt.values) + "\n"
    print snippet
    if options.algorithm == "LBFGS":
        if options.test_gradient.lower() == "true":
            gradient_check_lbfgs()
        else:
            print 'skipping gradient check...'
            init_theta = initialize_theta(options.input_weights)
            t1 = minimize(get_likelihood, init_theta, method='L-BFGS-B', jac=get_gradient, tol=1e-4,
                          options={'maxiter': 300})
            theta = t1.x
    elif options.algorithm == "EM":
        if options.test_gradient.lower() == "true":
            gradient_check_em()
        else:
            print 'skipping gradient check...'
            theta = initialize_theta(options.input_weights)
            new_e = get_likelihood(theta)
            exp_new_e = get_likelihood_with_expected_counts(theta)
            old_e = float('-inf')
            converged = False
            while not converged:
                t1 = minimize(get_likelihood_with_expected_counts, theta, method='L-BFGS-B', jac=get_gradient, tol=1e-3,
                              options={'maxiter': 150})
                theta = t1.x
                new_e = get_likelihood(theta)  # this will also update expected counts
                converged = round(abs(old_e - new_e), 2) == 0.0
                old_e = new_e
    elif options.algorithm == "SGD":
        batch_size = len(trellis)
        theta = initialize_theta(options.input_weights)
        get_likelihood(theta, display=True)
        reset_fractional_counts()
        batch_idxs = np.array_split(range(len(trellis)), len(trellis) / batch_size)
        for iter in xrange(3):
            random.shuffle(batch_idxs)
            b_id = 0
            for batch_idx in batch_idxs:
                print b_id
                b_id += 1
                t1 = minimize(get_likelihood, theta, method='L-BFGS-B', jac=get_gradient, args=(batch_idx, False),
                              tol=1e-5,
                              options={'maxiter': 150})
                theta = t1.x
            get_likelihood(theta, display=True)
    else:
        print 'wrong option for algorithm...'
        exit()

    write_logs(theta, current_iter=None)
