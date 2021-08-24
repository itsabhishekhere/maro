# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from abc import ABC, abstractmethod
from collections import defaultdict
from multiprocessing import Pipe, Process
from os import getcwd
from random import choices
from typing import Callable

from maro.communication.patterns import ManagerEndpoint
from maro.rl.utils import MsgTag
from maro.rl.wrappers import AbsEnvWrapper, AgentWrapper
from maro.utils import Logger, set_seeds

from .common import get_rollout_finish_msg
from .environment_sampler import EnvironmentSampler


class AbsRolloutManager(ABC):
    """Controller for simulation data collection.

    Args:
        post_collect (Callable): Custom function to process whatever information is collected by each
            environment wrapper (local or remote) at the end of ``collect`` calls. The function signature should
            be (trackers, ep, segment) -> None, where tracker is a list of environment wrappers' ``tracker`` members.
            Defaults to None.
        post_evaluate (Callable): Custom function to process whatever information is collected by each
            environment wrapper (local or remote) at the end of ``evaluate`` calls. The function signature should
            be (trackers, ep) -> None, where tracker is a list of environment wrappers' ``tracker`` members. Defaults
            to None.
    """
    def __init__(self, post_collect: Callable = None, post_evaluate: Callable = None):
        super().__init__()
        self._post_collect = post_collect
        self._post_evaluate = post_evaluate
        self.episode_complete = False

    @abstractmethod
    def collect(self, ep: int, segment: int, policy_state_dict: dict, version: int):
        """Collect simulation data, i.e., experiences for training.

        Args:
            ep (int): Current episode index.
            segment (int): Current segment index.
            policy_state_dict (dict): Policy states to use for simulation.
            version (int): Version index from the policy manager from which the ``policy_state_dict`` is obtained.

        Returns:
            Experiences for policy training.
        """
        raise NotImplementedError

    @abstractmethod
    def evaluate(self, ep: int, policy_state_dict: dict):
        """Evaluate the performance of ``policy_state_dict``.

        Args:
            ep (int): Current training episode index.
            policy_state_dict (dict): Policy states to use for simulation.

        Returns:
            Environment summary.
        """
        raise NotImplementedError

    def reset(self):
        self.episode_complete = False


class SimpleRolloutManager(AbsRolloutManager):
    """Local roll-out controller.

    Args:
        get_env_wrapper (Callable): Function to create an environment wrapper for collecting training data. The function
            should take no parameters and return an environment wrapper instance.
        get_agent_wrapper (Callable): Function to create an agent wrapper that interacts with the environment wrapper.
            The function should take no parameters and return a ``AgentWrapper`` instance.
        num_steps (int): Number of environment steps to roll out in each call to ``collect``. Defaults to -1, in which
            case the roll-out will be executed until the end of the environment.
        get_eval_env_wrapper (Callable): Function to create an environment wrapper for evaluation. The function should
            take no parameters and return an environment wrapper instance. If this is None, the training environment
            wrapper will be used for evaluation in the worker processes. Defaults to None.
        post_collect (Callable): Custom function to process whatever information is collected by each
            environment wrapper (local or remote) at the end of ``collect`` calls. The function signature should
            be (trackers, ep, segment) -> None, where tracker is a list of environment wrappers' ``tracker`` members.
            Defaults to None.
        post_evaluate (Callable): Custom function to process whatever information is collected by each
            environment wrapper (local or remote) at the end of ``evaluate`` calls. The function signature should
            be (trackers, ep) -> None, where tracker is a list of environment wrappers' ``tracker`` members. Defaults
            to None.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "ROLLOUT_MANAGER" will be created at init
            time and this directory will be used to save the log files generated by it. Defaults to the current
            working directory.
    """
    def __init__(
        self,
        get_env_wrapper: Callable[[], AbsEnvWrapper],
        get_agent_wrapper: Callable[[], AgentWrapper],
        num_steps: int = -1,
        parallelism: int = 1,
        get_eval_env_wrapper: Callable[[], AbsEnvWrapper] = None,
        eval_parallelism: int = 1,
        post_collect: Callable = None,
        post_evaluate: Callable = None,
        log_dir: str = getcwd()
    ):
        if num_steps == 0 or num_steps < -1:
            raise ValueError("num_steps must be a positive integer or -1.")

        if parallelism < 1:
            raise ValueError("'parallelism' must be equal to or greater than 1.")

        if eval_parallelism > parallelism:
            raise ValueError("'num_eval_workers' can not be greater than 'parallelism'.")

        super().__init__(post_collect=post_collect, post_evaluate=post_evaluate)
        self._logger = Logger("ROLLOUT_MANAGER", dump_folder=log_dir)
        self._num_steps = num_steps if num_steps > 0 else float("inf")
        self._exploration_step = False
        self._parallelism = parallelism
        self._eval_parallelism = eval_parallelism
        if self._parallelism == 1:
            self.env_sampler = EnvironmentSampler(
                get_env_wrapper, get_agent_wrapper, get_eval_env_wrapper=get_eval_env_wrapper
            )
        else:
            self._worker_processes = []
            self._manager_ends = []

            def _rollout_worker(index, conn, get_env_wrapper, get_agent_wrapper, get_eval_env_wrapper=None):
                set_seeds(index)
                env_sampler = EnvironmentSampler(
                    get_env_wrapper, get_agent_wrapper, get_eval_env_wrapper=get_eval_env_wrapper
                )
                logger = Logger("ROLLOUT_WORKER", dump_folder=log_dir)
                while True:
                    msg = conn.recv()
                    if msg["type"] == "sample":
                        result = env_sampler.sample(
                            policy_state_dict=msg["policy_state"],
                            num_steps=self._num_steps,
                            exploration_step=self._exploration_step
                        )
                        logger.info(get_rollout_finish_msg(
                            msg["episode"], result["step_range"], exploration_params=result["exploration_params"]
                        ))
                        result["worker_index"] = index
                        conn.send(result)
                    elif msg["type"] == "test":
                        tracker = env_sampler.test(msg["policy_state"])
                        logger.info("Evaluation...")
                        conn.send({"worker_id": index, "tracker": tracker})
                    elif msg["type"] == "quit":
                        break

            for index in range(self._parallelism):
                manager_end, worker_end = Pipe()
                self._manager_ends.append(manager_end)
                worker = Process(
                    target=_rollout_worker,
                    args=(index, worker_end, get_env_wrapper, get_agent_wrapper),
                    kwargs={"get_eval_env_wrapper": get_eval_env_wrapper}
                )
                self._worker_processes.append(worker)
                worker.start()

    def collect(self, ep: int, segment: int, policy_state_dict: dict, version: int):
        """Collect simulation data, i.e., experiences for training.

        Args:
            ep (int): Current episode index.
            segment (int): Current segment index.
            policy_state_dict (dict): Policy states to use for simulation.
            version (int): Version index from the policy manager from which the ``policy_state_dict`` is obtained.

        Returns:
            Experiences for policy training.
        """
        self._logger.info(f"Collecting simulation data (episode {ep}, policy version {version})")

        info_by_policy, trackers = defaultdict(list), []
        if self._parallelism == 1:
            result = self.env_sampler.sample(
                policy_state_dict=policy_state_dict,
                num_steps=self._num_steps,
                exploration_step=self._exploration_step
            )
            self._logger.info(
                get_rollout_finish_msg(ep, result["step_range"], exploration_params=result["exploration_params"])
            )

            for policy_name, info in result["rollout_info"].items():
                info_by_policy[policy_name].append(info)
            trackers.append(result["tracker"])
            self.episode_complete = result["end_of_episode"]
        else:
            rollout_req = {
                "type": "sample",
                "episode": ep,
                "num_steps": self._num_steps,
                "policy_state": policy_state_dict,
                "exploration_step": self._exploration_step
            }

            for conn in self._manager_ends:
                conn.send(rollout_req)

            if self._exploration_step:
                self._exploration_step = False

            for conn in self._manager_ends:
                result = conn.recv()
                for policy_name, info in result["rollout_info"].items():
                    info_by_policy[policy_name].append(info)
                trackers.append(result["tracker"])
                self.episode_complete = result["episode_end"]

        if self.episode_complete:
            self._exploration_step = True

        if self._post_collect:
            self._post_collect(trackers, ep, segment)

        return info_by_policy

    def evaluate(self, ep: int, policy_state_dict: dict):
        """Evaluate the performance of ``policy_state_dict``.

        Args:
            ep (int): Current training episode index.
            policy_state_dict (dict): Policy states to use for simulation.

        Returns:
            Environment summary.
        """
        trackers = []
        if self._eval_parallelism == 1:
            self._logger.info("Evaluating...")
            tracker = self.env_sampler.test(policy_state_dict)
            trackers.append(tracker)
        else:
            eval_worker_conns = choices(self._manager_ends, k=self._eval_parallelism)
            for conn in eval_worker_conns:
                conn.send({"type": "test", "episode": ep, "policy_state": policy_state_dict})

            for conn in self._manager_ends:
                result = conn.recv()
                trackers.append(result["tracker"])

        if self._post_evaluate:
            self._post_evaluate(trackers, ep)

        return trackers

    def exit(self):
        """Tell the worker processes to exit."""
        if self._parallelism > 1:
            for conn in self._manager_ends:
                conn.send({"type": "quit"})


class DistributedRolloutManager(AbsRolloutManager):
    """Controller for a set of remote roll-out workers, possibly distributed on different computation nodes.

    Args:
        group (str): Group name for the roll-out cluster, which includes all roll-out workers and a roll-out manager
            that manages them.
        num_workers (int): Number of remote roll-out workers.
        num_steps (int): Number of environment steps to roll out in each call to ``collect``. Defaults to -1, in which
            case the roll-out will be executed until the end of the environment.
        min_finished_workers (int): Minimum number of finished workers required for a ``collect`` call. Defaults to
            None, in which case it will be set to ``num_workers``.
        max_extra_recv_tries (int): Maximum number of attempts to receive worker results after ``min_finished_workers``
            have been received in ``collect``. Defaults to None, in which case it is set to ``num_workers`` -
            ``min_finished_workers``.
        extra_recv_timeout (int): Timeout (in milliseconds) for each attempt to receive from a worker after
            ``min_finished_workers`` have been received in ``collect``. Defaults to 100 (milliseconds).
        max_lag (int): Maximum policy version lag allowed for experiences collected from remote roll-out workers.
            Experiences collected using policy versions older than (current_version - max_lag) will be discarded.
            Defaults to 0, in which case only experiences collected using the latest policy version will be returned.
        num_eval_workers (int): Number of workers for evaluation. Defaults to 1.
        post_collect (Callable): Custom function to process whatever information is collected by each
            environment wrapper (local or remote) at the end of ``collect`` calls. The function signature should
            be (trackers, ep, step_range) -> None, where tracker is a list of environment wrappers' ``tracker`` members.
            Defaults to None.
        post_evaluate (Callable): Custom function to process whatever information is collected by each
            environment wrapper (local or remote) at the end of ``evaluate`` calls. The function signature should
            be (trackers, ep) -> None, where tracker is a list of environment wrappers' ``tracker`` members. Defaults
            to None.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "ROLLOUT_MANAGER" will be created at init
            time and this directory will be used to save the log files generated by it. Defaults to the current
            working directory.
        endpoint_kwargs: Keyword parameters for the internal ``ManagerEndpoint`` instance. See ``ManagerEndpoint`` class
            for details. Defaults to an empty dictionary.
    """
    def __init__(
        self,
        group: str,
        num_workers: int,
        num_steps: int = -1,
        min_finished_workers: int = None,
        max_extra_recv_tries: int = None,
        extra_recv_timeout: int = None,
        max_lag: int = 0,
        num_eval_workers: int = 1,
        post_collect: Callable = None,
        post_evaluate: Callable = None,
        log_dir: str = getcwd(),
        endpoint_kwargs: dict = {}
    ):
        if num_eval_workers > num_workers:
            raise ValueError("num_eval_workers cannot exceed the number of available workers")

        super().__init__(post_collect=post_collect, post_evaluate=post_evaluate)
        self._num_workers = num_workers
        self._endpoint = ManagerEndpoint(group, "ROLLOUT_MANAGER", num_workers, **endpoint_kwargs)
        self._logger = Logger("ROLLOUT_MANAGER", dump_folder=log_dir)

        self._num_steps = num_steps
        if min_finished_workers is None:
            min_finished_workers = self._num_workers
            self._logger.info(f"Minimum number of finished workers is set to {min_finished_workers}")

        self._min_finished_workers = min_finished_workers

        if max_extra_recv_tries is None:
            max_extra_recv_tries = self._num_workers - self._min_finished_workers
            self._logger.info(f"Maximum number of extra receive tries is set to {max_extra_recv_tries}")

        self._max_extra_recv_tries = max_extra_recv_tries
        self._extra_recv_timeout = extra_recv_timeout

        self._max_lag = max_lag
        self._num_eval_workers = num_eval_workers
        self._exploration_step = False

    def collect(self, ep: int, segment: int, policy_state_dict: dict, version: int):
        """Collect simulation data, i.e., experiences for training.

        Args:
            ep (int): Current episode index.
            segment (int): Current segment index.
            policy_state_dict (dict): Policy states to use for simulation.
            version (int): Version index from the policy manager from which the ``policy_state_dict`` is obtained.

        Returns:
            Experiences for policy training.
        """
        for worker_id in self._endpoint.workers:
            self._endpoint.send(
                worker_id,
                {
                    "type": MsgTag.SAMPLE,
                    "episode": ep,
                    "segment": segment,
                    "num_steps": self._num_steps,
                    "policy_state": policy_state_dict,
                    "version": version,
                    "exploration_step": self._exploration_step
                }
            )

        self._logger.info(f"Collecting simulation data (episode {ep}, segment {segment}, policy version {version})")

        if self._exploration_step:
            self._exploration_step = False

        info_list_by_policy, trackers, num_finishes = defaultdict(list), [], 0
        # Ensure the minimum number of worker results are received.
        while True:
            result, worker_id = self._endpoint.receive()
            info_by_policy, tracker = self._handle_worker_result(result, ep, segment, version)
            if info_by_policy:
                num_finishes += 1
                for policy_name, info in info_by_policy.items():
                    info_list_by_policy[policy_name].append(info)
                trackers.append(tracker)
            if num_finishes == self._min_finished_workers:
                break

        # Keep trying to receive from workers, but with timeout
        for i in range(self._max_extra_recv_tries):
            received = self._endpoint.receive(timeout=self._extra_recv_timeout)
            if not received:
                self._logger.info(f"{self._max_extra_recv_tries - i - 1} attempts left")
            else:
                result, worker_id = received
                info_by_policy, tracker = self._handle_worker_result(result, ep, segment, version)
                if info_by_policy:
                    num_finishes += 1
                    for policy_name, info in info_by_policy.items():
                        info_list_by_policy[policy_name].append(info)
                    trackers.append(tracker)
                if num_finishes == self._num_workers:
                    break

        if self.episode_complete:
            self._exploration_step = True

        if self._post_collect:
            self._post_collect(trackers, ep, segment)

        return info_list_by_policy

    def _handle_worker_result(self, msg, ep, segment, version):
        if msg["type"] != MsgTag.SAMPLE_DONE:
            self._logger.info(
                f"Ignored a message of type {msg['type']} (expected message type {MsgTag.SAMPLE_DONE})"
            )
            return None, None

        if version - msg["version"] > self._max_lag:
            self._logger.info(
                f"Ignored a message because it contains experiences generated using a stale policy version. "
                f"Expected experiences generated using policy versions no earlier than {version - self._max_lag} "
                f"got {msg['version']}"
            )
            return None, None

        # The message is what we expect
        if msg["episode"] == ep and msg["segment"] == segment:
            self.episode_complete = msg["end_of_episode"]
            return msg["rollout_info"], msg["tracker"]

        return None, None

    def evaluate(self, ep: int, policy_state_dict: dict):
        """Evaluate the performance of ``policy_state_dict``.

        Args:
            ep (int): Current training episode index.
            policy_state_dict (dict): Policy states to use for simulation.

        Returns:
            Environment summary.
        """
        workers = choices(self._endpoint.workers, k=self._num_eval_workers)
        for worker_id in self._endpoint.workers:
            self._endpoint.send(worker_id, {"type": MsgTag.EVAL, "episode": ep, "policy_state": policy_state_dict})
        self._logger.info(f"Sent evaluation requests to {workers}")

        # Receive evaluation results from remote workers
        num_finishes, trackers = 0, []
        for _ in range(len(workers)):
            result, worker_id = self._endpoint.receive()
            if result["type"] != MsgTag.TEST_DONE or result["episode"] != ep:
                self._logger.info(
                    f"Ignore a message of type {result['type']} with episode index {result['episode']} "
                    f"(expected message type {MsgTag.TEST_DONE} and episode index {ep})"
                )
                continue

            trackers.append(result["tracker"])

            if result["episode"] == ep:
                num_finishes += 1
                if num_finishes == self._num_eval_workers:
                    break

        if self._post_evaluate:
            self._post_evaluate(trackers, ep)

        return trackers

    def exit(self):
        """Tell the remote workers to exit."""
        self._endpoint.exit()
