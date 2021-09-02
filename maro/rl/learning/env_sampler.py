# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from collections import defaultdict, deque
from multiprocessing import Pipe, Process
from os import getcwd
from typing import Callable, Dict, List

import numpy as np

from maro.communication import Proxy, SessionMessage, SessionType
from maro.rl.policy import RLPolicy
from maro.rl.utils import MsgKey, MsgTag
from maro.simulator import Env
from maro.utils import Logger

from .helpers import get_rollout_finish_msg


class AgentWrapper:
    def __init__(
        self,
        get_policy_func_dict: Dict[str, Callable],
        agent2policy: Dict[str, str],
        policies_to_parallelize: List[str] = []
    ):
        self._policies_to_parallelize = set(policies_to_parallelize)
        self.policy_dict = {
            id_: func(id_) for id_, func in get_policy_func_dict.items() if id_ not in self._policies_to_parallelize
        }
        self.agent2policy = agent2policy
        self.policy_by_agent = {
            agent: self.policy_dict[policy_id] for agent, policy_id in agent2policy.items()
            if policy_id in self.policy_dict
        }

        self._policy_hosts = []
        self._conn = {}

        def _policy_host(name, get_policy, conn):
            policy = get_policy(name)
            while True:
                msg = conn.recv()
                if msg["type"] == "choose_action":
                    actions = policy.choose_action(msg["states"])
                    conn.send(actions)
                elif msg["type"] == "set_state":
                    policy.set_state(msg["policy_state"])
                elif msg["type"] == "explore":
                    policy.explore()
                elif msg["type"] == "exploit":
                    policy.exploit()
                elif msg["type"] == "exploration_step":
                    policy.exploration_step()
                elif msg["type"] == "rollout_info":
                    conn.send(policy.get_rollout_info())
                elif msg["type"] == "exploration_params":
                    conn.send(policy.exploration_params)
                elif msg["type"] == "record":
                    policy.record(
                        msg["agent"], msg["state"], msg["action"], msg["reward"], msg["next_state"], msg["terminal"]
                    )
                elif msg["type"] == "update":
                    policy.update(msg["loss_info"])
                elif msg["type"] == "learn":
                    policy.learn(msg["batch"])

        for id_ in policies_to_parallelize:
            conn1, conn2 = Pipe()
            self._conn[id_] = conn1
            host = Process(target=_policy_host, args=(id_, get_policy_func_dict[id_], conn2))
            self._policy_hosts.append(host)
            host.start()

    def choose_action(self, state_by_agent: Dict[str, np.ndarray]):
        states_by_policy, agents_by_policy, action = defaultdict(list), defaultdict(list), {}
        for agent, state in state_by_agent.items():
            if self.agent2policy[agent] in self._conn:
                states_by_policy[self.agent2policy[agent]].append(state)
                agents_by_policy[self.agent2policy[agent]].append(agent)

        for policy_id, states in states_by_policy.items():
            self._conn[policy_id].send({"type": "choose_action", "states": np.concatenate(states)})

        for policy_id, states in states_by_policy.items():
            msg = self._conn[policy_id].recv()
            if len(states) == 1:
                msg = [msg]
            for agent, act in zip(agents_by_policy[policy_id], msg):
                action[agent] = act

        return {
            **action,
            **{
                agent: self.policy_by_agent[agent].choose_action(state) for agent, state in state_by_agent.items()
                if agent in self.policy_by_agent
            }
        }

    def set_policy_states(self, policy_state_dict: dict):
        for policy_id, conn in self._conn.items():
            conn.send({"type": "set_state", "policy_state": policy_state_dict[policy_id]})
        for policy_id, policy_state in policy_state_dict.items():
            if policy_id not in self._conn:
                self.policy_dict[policy_id].set_state(policy_state)

    def explore(self):
        for conn in self._conn.values():
            conn.send({"type": "explore"})
        for policy in self.policy_dict.values():
            if hasattr(policy, "explore"):
                policy.explore()

    def exploit(self):
        for conn in self._conn.values():
            conn.send({"type": "exploit"})
        for policy in self.policy_dict.values():
            if hasattr(policy, "exploit"):
                policy.exploit()

    def exploration_step(self):
        for conn in self._conn.values():
            conn.send({"type": "exploration_step"})
        for policy in self.policy_dict.values():
            if hasattr(policy, "exploration_step"):
                policy.exploration_step()

    def get_rollout_info(self):
        for conn in self._conn.values():
            conn.send({"type": "rollout_info"})

        return {
            **{
                id_: policy.get_rollout_info() for id_, policy in self.policy_dict.items()
                if isinstance(policy, RLPolicy)
            },
            **{id_: conn.recv() for id_, conn in self._conn.items()}
        }

    def get_exploration_params(self):
        for conn in self._conn.values():
            conn.send({"type": "exploration_params"})

        return {
            **{
                id_: policy.exploration_params for id_, policy in self.policy_dict.items()
                if isinstance(policy, RLPolicy)
            },
            **{id_: conn.recv() for id_, conn in self._conn.items()}
        }

    def record_transition(self, agent: str, state, action, reward, next_state, terminal: bool):
        if agent in self.policy_by_agent:
            if isinstance(self.policy_by_agent[agent], RLPolicy):
                self.policy_by_agent[agent].record(agent, state, action, reward, next_state, terminal)
        else:
            self._conn[self.agent2policy[agent]].send({
                "type": "record", "agent": agent, "state": state, "action": action, "reward": reward,
                "next_state": next_state, "terminal": terminal
            })


class EnvSampler:
    """Simulation data collector and policy evaluator.

    Args:
        get_env (Callable[[], Env]): Function to create an ``Env`` instance for collecting training data. The function
            should take no parameters and return an environment wrapper instance.
        get_policy_func_dict (dict): A dictionary mapping policy names to functions that create them. The policy
            creation function should have policy name as the only parameter and return an ``AbsPolicy`` instance.
        agent2policy (Dict[str, str]): A dictionary that maps agent IDs to policy IDs, i.e., specifies the policy used
            by each agent.
        get_state (Callable): Function to compute the state. The function takes as input an ``Env`` and an event and
            returns a state vector encoded as a one-dimensional (flattened) Numpy arrays for each agent involved as a
            dictionary.
        get_env_actions (Callable): Function to convert policy outputs to action objects that can be passed directly to
            the environment's ``step`` method. The function takes as input an ``Env``, a dictionary of a set of agents'
            policy output and an event and returns a list of action objects.
        get_reward (Callable): Function to compute rewards for a list of actions that occurred at a given tick. The
            function takes as input an ``Env``, a list of actions (output by ``get_env_actions``) and a tick and returns
            a scalar reward for each agent as a dictionary.
        get_test_env (Callable): Function to create an ``Env`` instance for testing policy performance. The function
            should take no parameters and return an environment wrapper instance. If this is None, the training
            environment wrapper will be used for evaluation in the worker processes. Defaults to None.
        reward_eval_delay (int): Number of ticks required after a decision event to evaluate the reward
            for the action taken for that event. Defaults to 0, which means rewards are evaluated immediately
            after executing an action.
        post_step (Callable): Custom function to gather information about a transition and the evolvement of the
            environment. The function signature should be (env, tracker, transition) -> None, where env is the ``Env``
            instance in the wrapper, tracker is a dictionary where the gathered information is stored and transition
            is a ``Transition`` object. For example, this callback can be used to collect various statistics on the
            simulation. Defaults to None.
    """
    def __init__(
        self,
        get_env: Callable[[], Env],
        get_policy_func_dict: Dict[str, Callable],
        agent2policy: Dict[str, str],
        get_state: Dict[str, Callable],
        get_env_actions: Callable,
        get_reward: Callable,
        get_test_env: Callable[[], Env] = None,
        reward_eval_delay: int = 0,
        post_step: Callable = None,
        policies_to_parallelize: List[str] = []
    ):
        self._learn_env = get_env()
        self._test_env = get_test_env() if get_test_env else self._learn_env
        self.env = None

        self.agent_wrapper = AgentWrapper(
            get_policy_func_dict, agent2policy, policies_to_parallelize=policies_to_parallelize
        )

        self.reward_eval_delay = reward_eval_delay
        self._post_step = post_step

        # shaping
        self._get_state = get_state
        self._get_env_actions = get_env_actions
        self._get_reward = get_reward

        self._state = None
        self._event = None
        self._step_index = 0
        self._terminal = True

        self._transition_cache = defaultdict(deque)  # for caching transitions whose rewards have yet to be evaluated
        self.tracker = {}  # User-defined tracking information is placed here.

    def sample(self, policy_state_dict: dict = None, num_steps: int = -1, exploration_step: bool = False):
        self.env = self._learn_env
        # set policy states
        if policy_state_dict:
            self.agent_wrapper.set_policy_states(policy_state_dict)

        # update exploration states if necessary
        self.agent_wrapper.explore()

        if exploration_step:
            self.agent_wrapper.exploration_step()

        if self._terminal:
            # reset and get initial state
            self.env.reset()
            self._step_index = 0
            self._transition_cache.clear()
            self.tracker.clear()
            self._terminal = False
            _, self._event, _ = self.env.step(None)
            self._state = self._get_state(self.env, self._event)

        starting_step_index = self._step_index + 1
        steps_to_go = float("inf") if num_steps == -1 else num_steps
        while not self._terminal and steps_to_go > 0:
            action = self.agent_wrapper.choose_action(self._state)
            env_actions = self._get_env_actions(self.env, action, self._event)
            for agent, state in self._state.items():
                self._transition_cache[agent].append((state, action[agent], env_actions, self.env.tick))
            _, self._event, self._terminal = self.env.step(env_actions)
            self._state = None if self._terminal else self._get_state(self.env, self._event)
            self._step_index += 1
            steps_to_go -= 1

        """
        If this is the final step, evaluate rewards for all remaining events except the last.
        Otherwise, evaluate rewards only for events at least self.reward_eval_delay ticks ago.
        """
        for agent, cache in self._transition_cache.items():
            while cache and (self._terminal or self.env.tick - cache[0][-1] >= self.reward_eval_delay):
                state, action, env_actions, tick = cache.popleft()
                reward = self._get_reward(self.env, env_actions, tick)
                if self._post_step:
                    # put things you want to track in the tracker attribute
                    self._post_step(self.env, self.tracker, state, action, env_actions, reward, tick)

                self.agent_wrapper.record_transition(
                    agent, state, action, reward[agent], cache[0][0] if cache else self._state,
                    not cache and self._terminal
                )

        return {
            "rollout_info": self.agent_wrapper.get_rollout_info(),
            "step_range": (starting_step_index, self._step_index),
            "tracker": self.tracker,
            "end_of_episode": self._terminal,
            "exploration_params": self.agent_wrapper.get_exploration_params()
        }

    def test(self, policy_state_dict: dict = None):
        self.env = self._test_env
        # set policy states
        if policy_state_dict:
            self.agent_wrapper.set_policy_states(policy_state_dict)

        # Set policies to exploitation mode
        self.agent_wrapper.exploit()

        self.env.reset()
        terminal = False
        # get initial state
        _, event, _ = self.env.step(None)
        state = self._get_state(self.env, event)
        while not terminal:
            action = self.agent_wrapper.choose_action(state)
            env_actions = self._get_env_actions(self.env, action, event)
            _, event, terminal = self.env.step(env_actions)
            if not terminal:
                state = self._get_state(self.env, event)

        return self.tracker

    def worker(self, group: str, index: int, proxy_kwargs: dict = {}, log_dir: str = getcwd()):
        """Roll-out worker process that can be launched on separate computation nodes.

        Args:
            group (str): Group name for the roll-out cluster, which includes all roll-out workers and a roll-out manager
                that manages them.
            worker_idx (int): Worker index. The worker's ID in the cluster will be "ROLLOUT_WORKER.{worker_idx}".
                This is used for bookkeeping by the parent manager.
            proxy_kwargs: Keyword parameters for the internal ``Proxy`` instance. See ``Proxy`` class
                for details. Defaults to the empty dictionary.
            log_dir (str): Directory to store logs in. Defaults to the current working directory.
        """
        proxy = Proxy(
            group, "rollout_worker", {"rollout_manager": 1}, component_name=f"ROLLOUT_WORKER.{index}", **proxy_kwargs
        )
        logger = Logger(proxy.name, dump_folder=log_dir)

        """
        The event loop handles 3 types of messages from the roll-out manager:
            1)  COLLECT, upon which the agent-environment simulation will be carried out for a specified number of steps
                and the collected experiences will be sent back to the roll-out manager;
            2)  EVAL, upon which the policies contained in the message payload will be evaluated for the entire
                duration of the evaluation environment.
            3)  EXIT, upon which it will break out of the event loop and the process will terminate.

        """
        for msg in proxy.receive():
            if msg.tag == MsgTag.EXIT:
                logger.info("Exiting...")
                proxy.close()
                break

            if msg.tag == MsgTag.SAMPLE:
                ep = msg.body[MsgKey.EPISODE]
                result = self.sample(
                    policy_state_dict=msg.body[MsgKey.POLICY_STATE],
                    num_steps=msg.body[MsgKey.NUM_STEPS],
                    exploration_step=msg.body[MsgKey.EXPLORATION_STEP]
                )
                logger.info(
                    get_rollout_finish_msg(ep, result["step_range"], exploration_params=result["exploration_params"])
                )
                return_info = {
                    MsgKey.EPISODE: ep,
                    MsgKey.SEGMENT: msg.body[MsgKey.SEGMENT],
                    MsgKey.VERSION: msg.body[MsgKey.VERSION],
                    MsgKey.ROLLOUT_INFO: result["rollout_info"],
                    MsgKey.STEP_RANGE: result["step_range"],
                    MsgKey.TRACKER: result["tracker"],
                    MsgKey.END_OF_EPISODE: result["end_of_episode"]
                }
                proxy.reply(msg, tag=MsgTag.SAMPLE_DONE, body=return_info)
            elif msg.tag == MsgTag.TEST:
                tracker = self.test(msg.body[MsgKey.POLICY_STATE])
                return_info = {MsgKey.TRACKER: tracker, MsgKey.EPISODE: msg.body[MsgKey.EPISODE]}
                logger.info("Testing complete")
                proxy.reply(msg, tag=MsgTag.TEST_DONE, body=return_info)

    def actor(
        self,
        group: str,
        index: int,
        num_episodes: int,
        num_steps: int = -1,
        proxy_kwargs: dict = {},
        log_dir: str = getcwd()
    ):
        """Controller for single-threaded learning workflows.

        Args:
            group (str): Group name for the cluster that includes the server and all actors.
            index (int): Integer actor index. The actor's ID in the cluster will be "ACTOR.{actor_idx}".
            num_episodes (int): Number of training episodes. Each training episode may contain one or more
                collect-update cycles, depending on how the implementation of the roll-out manager.
            num_steps (int): Number of environment steps to roll out in each call to ``collect``. Defaults to -1, in
                which case the roll-out will be executed until the end of the environment.
            proxy_kwargs: Keyword parameters for the internal ``Proxy`` instance. See ``Proxy`` class
                for details. Defaults to the empty dictionary.
            log_dir (str): Directory to store logs in. A ``Logger`` with tag "LOCAL_ROLLOUT_MANAGER" will be created at
                init time and this directory will be used to save the log files generated by it. Defaults to the current
                working directory.
        """
        if num_steps == 0 or num_steps < -1:
            raise ValueError("num_steps must be a positive integer or -1")

        peers = {"policy_server": 1}
        proxy = Proxy(group, "actor", peers, component_name=f"ACTOR.{index}", **proxy_kwargs)
        server_address = proxy.peers["policy_server"][0]
        logger = Logger(proxy.name, dump_folder=log_dir)

        # get initial policy states from the policy manager
        msg = SessionMessage(MsgTag.GET_INITIAL_POLICY_STATE, proxy.name, server_address)
        reply = proxy.send(msg)[0]
        policy_state_dict, policy_version = reply.body[MsgKey.POLICY_STATE], reply.body[MsgKey.VERSION]

        # main loop
        for ep in range(1, num_episodes + 1):
            exploration_step = True
            while True:
                result = self.sample(
                    policy_state_dict=policy_state_dict, num_steps=num_steps, exploration_step=exploration_step
                )
                logger.info(
                    get_rollout_finish_msg(ep, result["step_range"], exploration_params=result["exploration_params"])
                )
                # Send roll-out info to policy server for learning
                reply = proxy.send(
                    SessionMessage(
                        MsgTag.SAMPLE_DONE, proxy.name, server_address,
                        body={MsgKey.ROLLOUT_INFO: result["rollout_info"], MsgKey.VERSION: policy_version}
                    )
                )[0]
                policy_state_dict, policy_version = reply.body[MsgKey.POLICY_STATE], reply.body[MsgKey.VERSION]
                if result["end_of_episode"]:
                    break

                exploration_step = False

        # tell the policy server I'm all done.
        proxy.isend(SessionMessage(MsgTag.DONE, proxy.name, server_address, session_type=SessionType.NOTIFICATION))
        proxy.close()
