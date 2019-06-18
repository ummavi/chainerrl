from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()  # NOQA

import copy
from logging import getLogger

import numpy as np

import chainer
from chainer import cuda
import chainer.functions as F

from chainerrl.agents import DoubleDQN
from chainerrl.agents.dqn import compute_value_loss
from chainerrl.agents.dqn import compute_weighted_value_loss

from chainerrl.recurrent import state_kept
from chainerrl.misc.batch_states import batch_states
from chainerrl.replay_buffer import ReplayUpdater
from chainerrl.replay_buffer import PrioritizedReplayBuffer
from chainerrl.replay_buffer import PrioritizedBuffer


class PrioritizedDemoReplayBuffer(PrioritizedReplayBuffer):
    """Modification of a PrioritizedReplayBuffer to have both persistent
    demonstration data and normal demonstration data.

    Args:
        capacity(int): Capacity of the buffer *excluding* expert demonstrations

    Standard PER parameters:
        alpha, beta0, betasteps, eps (float)
        normalize_by_max (bool)
    """

    def __init__(self, capacity=None,
                 alpha=0.6, beta0=0.4, betasteps=2e5, eps=0.01,
                 normalize_by_max=True, error_min=0,
                 error_max=1, num_steps=1):

        PrioritizedReplayBuffer.__init__(self, capacity=None,
                                         alpha=0.6, beta0=0.4, betasteps=2e5,
                                         eps=0.01, normalize_by_max=True,
                                         error_min=0, error_max=1, num_steps=1)

        self.memory = PrioritizedBuffer(capacity)
        self.memory_demo = PrioritizedBuffer(None)

    def sample_from_memory(self, memory_str, m):
        """Samples `m` experiences from memory

        Args:
            m (int): Number of samples to draw
            memory_str (str)["agent"/"demo"]: Selects which memory to sample
        """
        memory = self.memory if memory_str == "agent" else self.memory_demo
        assert len(memory) >= m
        if m == 0:
            return []

        sampled, probabilities, min_prob = memory.sample(m)
        weights = self.weights_from_probabilities(probabilities, min_prob)
        for e, w in zip(sampled, weights):
            e[0]['weight'] = w
        return sampled

    def sample(self, n, demo_only=False):
        """Sample `n` experiences from memory.

        Args:
            n (int): Number of experiences to sample
            demo_only (bool): Force all samples to be drawn from demo buffer
        """
        if demo_only:
            sampled_demo = self.sample_from_memory("demo", n)
            return sampled_demo

        psum_agent = self.memory.priority_sums.sum()
        psum_demo = self.memory_demo.priority_sums.sum()
        psample_agent = psum_agent / (psum_agent + psum_demo)

        nsample_agent = np.random.binomial(n, psample_agent)
        # If we don't have enough RL transitions yet, force more demos
        nsample_agent = min(nsample_agent, len(self.memory))
        nsample_demo = n - nsample_agent

        sampled_agent = self.sample_from_memory("agent", nsample_agent)
        sampled_demo = self.sample_from_memory("demo", nsample_demo)

        return sampled_agent, sampled_demo

    def update_errors(self, errors_agent, errors_demo):
        if len(errors_demo) > 0:
            self.memory_demo.set_last_priority(
                self.priority_from_errors(errors_demo))
        if len(errors_agent) > 0:
            self.memory.set_last_priority(
                self.priority_from_errors(errors_agent))

    def append(self, state, action, reward, next_state=None, next_action=None,
               is_state_terminal=False, env_id=0, demo=False, **kwargs):
        """
        Args:
            demo: Flags transition as a demonstration and store it persistently
        """
        memory = self.memory_demo if demo else self.memory
        last_n_transitions = self.last_n_transitions[env_id]
        experience = dict(
            state=state,
            action=action,
            reward=reward,
            next_state=next_state,
            next_action=next_action,
            is_state_terminal=is_state_terminal,
            **kwargs
        )
        last_n_transitions.append(experience)
        if is_state_terminal:
            while last_n_transitions:
                memory.append(list(last_n_transitions))
                del last_n_transitions[0]
            assert len(last_n_transitions) == 0
        else:
            if len(last_n_transitions) == self.num_steps:
                memory.append(list(last_n_transitions))

    def stop_current_episode(self, demo=False, env_id=0):
        memory = self.memory if demo is False else self.memory_demo
        last_n_transitions = self.last_n_transitions[env_id]
        # if n-step transition hist is not full, add transition;
        # if n-step hist is indeed full, transition has already been added;
        if 0 < len(last_n_transitions) < self.num_steps:
            memory.append(list(last_n_transitions))
        # avoid duplicate entry
        if 0 < len(last_n_transitions) <= self.num_steps:
            del last_n_transitions[0]
        while last_n_transitions:
            memory.append(list(last_n_transitions))
            del last_n_transitions[0]
        assert len(last_n_transitions) == 0

    def __len__(self):
        return len(self.memory)+len(self.memory_demo)


class DemoReplayUpdater(object):
    """Object that handles update schedule and configurations.

    Args:
        replay_buffer (PrioritizedDemoReplayBuffer): Replay buffer for self-play
        update_func (callable): Callable that accepts one of these:
            (1) two lists of transition dicts (if episodic_update=False)
            (2) two lists of transition dicts (if episodic_update=True)
        batchsize (int): Minibatch size
        update_interval (int): Model update interval in step
        n_times_update (int): Number of repetition of update
        episodic_update (bool): Use full episodes for update if set True
        episodic_update_len (int or None): Subsequences of this length are used
            for update if set int and episodic_update=True
    """

    def __init__(self, replay_buffer,
                 update_func, batchsize, episodic_update,
                 n_times_update, replay_start_size, update_interval,
                 episodic_update_len=None):
        assert batchsize <= replay_start_size
        self.replay_buffer = replay_buffer
        self.update_func = update_func
        self.batchsize = batchsize
        self.episodic_update = episodic_update
        self.episodic_update_len = episodic_update_len
        self.n_times_update = n_times_update
        self.replay_start_size = replay_start_size
        self.update_interval = update_interval

    def update_if_necessary(self, iteration):
        """Called during normal self-play
        """
        if len(self.replay_buffer) < self.replay_start_size:
            print("TOo small")
            return

        if (self.episodic_update and self.replay_buffer.n_episodes < self.batchsize):

            return

        if iteration % self.update_interval != 0:
            return

        for _ in range(self.n_times_update):
            if self.episodic_update:
                raise NotImplementedError()
                # episodes_agent = self.replay_buffer_agent.sample_episodes(
                # self.batchsize, self.episodic_update_len)
                # episodes_demo = self.replay_buffer_demo.sample_episodes(
                # self.batch_size, self.episodic_update_len)
                # epissodes_agent, episodes_demo = self.replay_buffer.sample(self.batch_size, )
                # self.update_func(episodes_agent, episodes_demo)
            else:
                transitions_agent, transitions_demo = self.replay_buffer.sample(
                    self.batchsize)
                self.update_func(transitions_agent, transitions_demo)

    def update_from_demonstrations(self):
        """Called during pre-train steps. All samples are from demo buffer
        """
        if self.episodic_update:
            episodes_demo = self.replay_buffer.sample_episodes(
                self.batch_size, self.episodic_update_len)
            self.update_func([], episodes_demo)
        else:
            transitions_demo = self.replay_buffer.sample(
                self.batchsize, demo_only=True)
            self.update_func([], transitions_demo)


def batch_experiences(experiences, xp, phi, gamma, batch_states=batch_states):
    """Takes a batch of k experiences each of which contains j
    consecutive transitions and vectorizes them, where j is between 1 and n.
    Args:
        experiences: list of experiences. Each experience is a list
            containing between 1 and n dicts containing
              - state (object): State
              - action (object): Action
              - reward (float): Reward
              - is_state_terminal (bool): True iff next state is terminal
              - next_state (object): Next state
        xp : Numpy compatible matrix library: e.g. Numpy or CuPy.
        phi : Preprocessing function
        gamma: discount factor
        batch_states: function that converts a list to a batch
    Returns:
        dict of batched transitions

    Changes from chainerrl.replay_buffer.batch_experiences:
        Calculates and stores both n_step and 1_step reward
    """

    batch_exp = {
        'state': batch_states(
            [elem[0]['state'] for elem in experiences], xp, phi),
        'action': xp.asarray([elem[0]['action'] for elem in experiences]),
        'reward_nstep': xp.asarray([sum((gamma ** i) * exp[i]['reward']
                                        for i in range(len(exp)))
                                    for exp in experiences],
                                   dtype=np.float32),
        'next_state_nstep': batch_states(
            [elem[-1]['next_state']
             for elem in experiences], xp, phi),

        'reward_1step': xp.asarray([exp[0]['reward']
                                    for exp in experiences],
                                   dtype=np.float32),
        'next_state_1step': batch_states(
            [elem[0]['next_state']
             for elem in experiences], xp, phi),

        'is_state_terminal': xp.asarray(
            [any(transition['is_state_terminal']
                 for transition in exp) for exp in experiences],
            dtype=np.float32),
        'discount': xp.asarray([(gamma ** len(elem))for elem in experiences],
                               dtype=np.float32)}
    if all(elem[-1]['next_action'] is not None for elem in experiences):
        batch_exp['next_action'] = xp.asarray(
            [elem[-1]['next_action'] for elem in experiences])
    return batch_exp


class DQfD(DoubleDQN):
    """Deep-Q Learning from Demonstrations
    See: https://arxiv.org/abs/1704.03732.

    TODO:
        * Test batch observe & train.
        * Test episodic update

    DQN Args:
        q_function (StateQFunction): Q-function
        optimizer (Optimizer): Optimizer that is already setup
        replay_buffer (PrioritizedDemoReplayBuffer): Replay buffer
        gamma (float): Discount factor
        explorer (Explorer): Explorer that specifies an exploration strategy.
        gpu (int): GPU device id if not None nor negative.
        replay_start_size (int): if the replay buffer's size is less than
            replay_start_size, skip update
        minibatch_size (int): Minibatch size
        update_interval (int): Model update interval in step
        target_update_interval (int): Target model update interval in step
        clip_delta (bool): Clip delta if set True
        phi (callable): Feature extractor applied to observations
        target_update_method (str): 'hard' or 'soft'.
        soft_update_tau (float): Tau of soft target update.
        n_times_update (int): Number of repetition of update
        average_q_decay (float): Decay rate of average Q, only used for
            recording statistics
        average_loss_decay (float): Decay rate of average loss, only used for
            recording statistics
        batch_accumulator (str): 'mean' or 'sum'
        episodic_update (bool): Use full episodes for update if set True
        episodic_update_len (int or None): Subsequences of this length are used
            for update if set int and episodic_update=True
        logger (Logger): Logger used
        batch_states (callable): method which makes a batch of observations.
            default is `chainerrl.misc.batch_states.batch_states`

    DQfD-specific args:
        n_pretrain_steps: Number of pretraining steps to perform
        demo_supervised_margin (float): Margin width for supervised demo loss
        loss_coeff_nstep(float): Coefficient used to regulate n-step q loss
        loss_coeff_supervised (float): Coefficient for the supervised loss term
        loss_coeff_l2 (float): Coefficient used to regulate weight decay rate
        bonus_priority_agent(float): Bonus priorities added to agent generated data
        bonus_priority_demo (float): Bonus priorities added to demonstration data
    """

    def __init__(self, q_function, optimizer,
                 replay_buffer,
                 gamma, explorer, n_pretrain_steps,
                 demo_supervised_margin=0.8,
                 bonus_priority_agent=0.001,
                 bonus_priority_demo=1.0,
                 loss_coeff_nstep=1.0,
                 loss_coeff_supervised=1.0,
                 loss_coeff_l2=1e-5, gpu=None,
                 replay_start_size=50000,
                 minibatch_size=32, update_interval=1,
                 target_update_interval=10000, clip_delta=True,
                 phi=lambda x: x,
                 target_update_method='hard',
                 soft_update_tau=1e-2,
                 n_times_update=1, average_q_decay=0.999,
                 average_loss_decay=0.99,
                 batch_accumulator='mean', episodic_update=False,
                 episodic_update_len=None,
                 logger=getLogger(__name__),
                 batch_states=batch_states):

        assert isinstance(replay_buffer, PrioritizedDemoReplayBuffer)
        super(DQfD, self).__init__(q_function, optimizer, replay_buffer, gamma,
                                   explorer, gpu, replay_start_size,
                                   minibatch_size, update_interval,
                                   target_update_interval, clip_delta,
                                   phi, target_update_method, soft_update_tau,
                                   n_times_update, average_q_decay,
                                   average_loss_decay, batch_accumulator,
                                   episodic_update, episodic_update_len,
                                   logger, batch_states)

        self.minibatch_size = minibatch_size
        self.n_pretrain_steps = n_pretrain_steps
        self.demo_supervised_margin = demo_supervised_margin
        self.loss_coeff_supervised = loss_coeff_supervised
        self.loss_coeff_l2 = loss_coeff_l2
        self.loss_coeff_nstep = loss_coeff_nstep
        self.bonus_priority_demo = bonus_priority_demo
        self.bonus_priority_agent = bonus_priority_agent

        self.optimizer.add_hook(
            chainer.optimizer_hooks.WeightDecay(loss_coeff_l2))

        # Overwrite DQN's replay updater.
        # TODO: Is there a better way to do this?
        self.replay_updater = DemoReplayUpdater(
            replay_buffer=self.replay_buffer,
            update_func=self.update,
            batchsize=minibatch_size,
            episodic_update=episodic_update,
            episodic_update_len=episodic_update_len,
            n_times_update=n_times_update,
            replay_start_size=replay_start_size,
            update_interval=update_interval,
        )

        # TODO: Should this really go here? Move into train function?
        self.pretrain()

    def pretrain(self):
        """Uses purely expert demonstrations to do pre-training
        """
        import tqdm
        for tpre in tqdm.tqdm(range(self.n_pretrain_steps)):
            self.replay_updater.update_from_demonstrations()
            if tpre % self.target_update_interval == 0:
                self.sync_target_network()
            print("Pretrain step",tpre, "Average Loss:", self.average_loss)

    def update(self, experiences_agent, experiences_demo):
        """Combined DQfD loss function for Demonstration and agent/RL.
        """
        num_exp_agent = len(experiences_agent)
        experiences = experiences_agent+experiences_demo
        exp_batch = batch_experiences(experiences, xp=self.xp, phi=self.phi,
                                      gamma=self.gamma,
                                      batch_states=self.batch_states)

        exp_batch['weights'] = self.xp.asarray(
            [elem[0]['weight']for elem in experiences], dtype=self.xp.float32)

        errors_out = []
        loss_q_nstep, loss_q_1step = self._compute_ddqn_losses(
            exp_batch, errors_out=errors_out)

        # Add the agent/demonstration bonus priorities and update
        err_agent, err_demo = errors_out[:num_exp_agent], errors_out[num_exp_agent:]
        err_agent = [e+self.bonus_priority_agent for e in err_agent]
        err_demo = [e+self.bonus_priority_demo for e in err_demo]
        self.replay_buffer.update_errors(err_agent, err_demo)

        # Large-margin supervised loss
        # Grab the cached Q(s) in the forward pass & subset demo exp.
        q_picked = self.qout.evaluate_actions(exp_batch["action"])
        q_expert_demos = q_picked[num_exp_agent:]

        # unwrap DiscreteActionValue and subset demos
        q_demos = self.qout.q_values[num_exp_agent:]

        # Calculate margin forall actions (l(a_E,a) in the paper)
        margin = np.zeros_like(q_demos.array) + self.demo_supervised_margin
        a_expert_demos = exp_batch["action"][num_exp_agent:]
        margin[np.arange(len(experiences_demo)), a_expert_demos] = 0.0

        supervised_targets = F.max(q_demos + margin, axis=-1)
        loss_supervised = F.sum(supervised_targets - q_expert_demos)
        if self.batch_accumulator is "mean":
            loss_supervised /= len(experiences_demo)

        # L2 loss is directly applied as chainer optimizer hook in init
        loss_combined = loss_q_1step + \
            self.loss_coeff_nstep * loss_q_nstep + \
            self.loss_coeff_supervised * loss_supervised

        self.model.cleargrads()
        loss_combined.backward()
        self.optimizer.update()


        # Update stats
        self.average_loss *= self.average_loss_decay
        self.average_loss += (1 - self.average_loss_decay) * \
            float(loss_combined.array)

    def _compute_y_and_ts(self, exp_batch):
        """Compute output and targets

        Changes from DQN:
            Cache qout for the supervised loss later
            Calculate both 1-step and n-step targets
        """
        batch_size = exp_batch['reward_nstep'].shape[0]

        # Compute Q-values for current states
        batch_state = exp_batch['state']
        qout = self.model(batch_state)

        # Caches Q(s) for use in supervised demo loss
        self.qout = qout

        batch_actions = exp_batch['action']
        batch_q = F.reshape(qout.evaluate_actions(
            batch_actions), (batch_size, 1))

        with chainer.no_backprop_mode():
            # Calculate n-step Double DQN targets
            # Rename n-step rewards and next states
            # .. to those _compute_target_values expects
            exp_batch["reward"] = exp_batch["reward_nstep"]
            exp_batch["next_state"] = exp_batch["next_state_nstep"]
            batch_q_target_nstep = F.reshape(
                self._compute_target_values(exp_batch),
                (batch_size, 1))

            # Calculate 1-step Double DQN targets
            exp_batch["reward"] = exp_batch["reward_1step"]
            exp_batch["next_state"] = exp_batch["next_state_1step"]
            batch_q_target_1step = F.reshape(
                self._compute_target_values(exp_batch),
                (batch_size, 1))

        return batch_q, batch_q_target_nstep, batch_q_target_1step

    def _compute_ddqn_losses(self, exp_batch, errors_out=None):
        """Compute the Q-learning losses for a batch of experiences

        Args:
          exp_batch (dict): A dict of batched arrays of transitions
        Returns:
          Computed loss from the minibatch of experiences
        """
        y, t_nstep, t_1step = self._compute_y_and_ts(exp_batch)

        # Calculate the errors_out for priorities with the 1-step err
        if errors_out is not None:
            del errors_out[:]
            delta = F.absolute(y - t_1step)
            if delta.ndim == 2:
                delta = F.sum(delta, axis=1)
            delta = cuda.to_cpu(delta.array)
            for e in delta:
                errors_out.append(e)

        if 'weights' in exp_batch:
            loss_1step = compute_weighted_value_loss(
                y, t_1step, exp_batch['weights'],
                clip_delta=self.clip_delta,
                batch_accumulator=self.batch_accumulator)
            loss_nstep = compute_weighted_value_loss(
                y, t_nstep, exp_batch['weights'],
                clip_delta=self.clip_delta,
                batch_accumulator=self.batch_accumulator)
            return loss_nstep, loss_1step
        else:
            loss_1step = compute_value_loss(y, t_1step,
                                            clip_delta=self.clip_delta,
                                            batch_accumulator=self.batch_accumulator)
            loss_nstep = compute_value_loss(y, t_nstep,
                                            clip_delta=self.clip_delta,
                                            batch_accumulator=self.batch_accumulator)
            return loss_nstep, loss_1step

    def act_and_train(self, obs, reward):

        with chainer.using_config('train', False), chainer.no_backprop_mode():
            action_value = self.model(
                self.batch_states([obs], self.xp, self.phi))
            q = float(action_value.max.array)
            greedy_action = cuda.to_cpu(action_value.greedy_actions.array)[0]

        # Update stats
        self.average_q *= self.average_q_decay
        self.average_q += (1 - self.average_q_decay) * q

        self.logger.debug('t:%s q:%s action_value:%s', self.t, q, action_value)

        action = self.explorer.select_action(
            self.t, lambda: greedy_action, action_value=action_value)
        self.t += 1

        # Update the target network
        if self.t % self.target_update_interval == 0:
            self.sync_target_network()

        if self.last_state is not None:
            assert self.last_action is not None
            # Add a transition to the replay buffer
            self.replay_buffer.append(
                state=self.last_state,
                action=self.last_action,
                reward=reward,
                next_state=obs,
                next_action=action,
                is_state_terminal=False)

        self.last_state = obs
        self.last_action = action

        self.replay_updater.update_if_necessary(self.t)
        self.logger.debug('t:%s r:%s a:%s', self.t, reward, action)

        return self.last_action

    def batch_observe_and_train(self, batch_obs, batch_reward,
                                batch_done, batch_reset):
        for i in range(len(batch_obs)):
            self.t += 1
            # Update the target network
            if self.t % self.target_update_interval == 0:
                self.sync_target_network()
            if self.batch_last_obs[i] is not None:
                assert self.batch_last_action[i] is not None
                # Add a transition to the replay buffer
                self.replay_buffer.append(
                    state=self.batch_last_obs[i],
                    action=self.batch_last_action[i],
                    reward=batch_reward[i],
                    next_state=batch_obs[i],
                    next_action=None,
                    is_state_terminal=batch_done[i],
                    env_id=i,
                )
                if batch_reset[i] or batch_done[i]:
                    self.batch_last_obs[i] = None
                    self.replay_buffer.stop_current_episode(env_id=i)
            self.replay_updater.update_if_necessary(self.t)