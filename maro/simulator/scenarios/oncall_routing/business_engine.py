import os
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from maro.backends.frame import FrameBase, SnapshotList
from yaml import safe_load

from maro.event_buffer import AtomEvent, CascadeEvent, EventBuffer, MaroEvents
from maro.simulator import Env
from maro.simulator.scenarios import AbsBusinessEngine
from maro.simulator.utils import random
from .arrival_time_predictor import EstimatedArrivalTimePredictor, ActualArrivalTimeSampler
from .carrier_status import CarrierStatus
from .common import Action, CarrierArrivalPayload, Events, OncallReceivePayload, OncallRoutingPayload, OrderId, \
    PlanElement, RouteNumber
from .data_loader import load_plan_simple
from .oncall_order_generator import FromCSVOncallOrderGenerator
from .order import Order
from .utils import GLOBAL_RAND_KEY


class OncallRoutingBusinessEngine(AbsBusinessEngine):
    def __init__(
        self,
        event_buffer: EventBuffer,
        topology: Optional[str],
        start_tick: int,
        max_tick: int,
        snapshot_resolution: int,
        max_snapshots: Optional[int],
        additional_options: dict = None
    ) -> None:
        super(OncallRoutingBusinessEngine, self).__init__(
            scenario_name="oncall_routing",
            event_buffer=event_buffer,
            topology=topology,
            start_tick=start_tick,
            max_tick=max_tick,
            snapshot_resolution=snapshot_resolution,
            max_snapshots=max_snapshots,
            additional_options=additional_options
        )

        self.update_config_root_path(__file__)
        with open(os.path.join(self._config_path, "config.yml")) as fp:
            self._config = safe_load(fp)

        self._frame = FrameBase()
        self._snapshots = self._frame.snapshots

        self._oncall_order_generator = FromCSVOncallOrderGenerator(
            os.path.join(self._config_path, "oncall_orders.csv")
        )
        self._oncall_order_buffer: Deque[Order] = deque()

        self._waiting_order_dict: Dict[OrderId, Order] = {}  # Orders already sent to agents and waiting for actions

        self._aat_predictor = ActualArrivalTimeSampler()
        self._eat_predictor = EstimatedArrivalTimePredictor()

        self._remain_plan: Dict[RouteNumber, List[PlanElement]] = load_plan_simple(
            os.path.join(self._config_path, "routes.csv")
        )
        self._routes: List[RouteNumber] = sorted(list(self._remain_plan.keys()))
        self._carrier_status: Dict[RouteNumber, CarrierStatus] = {}
        for route_number in self._remain_plan.keys():
            carrier_status = CarrierStatus()
            carrier_status.route_number = route_number
            carrier_status.coord = self._config["headquarter_coordinate"]
            # carrier_status.close_rtb = (16, 0)  # TODO
            self._carrier_status[route_number] = carrier_status
            for i in range(len(self._remain_plan[route_number])):
                self._refresh_arr_time(tick=-1, route_number=route_number, index=i)

        self._upcoming_arr_time: Dict[RouteNumber, Optional[int]] = {
            route_number: plan[0].act_arr_time for route_number, plan in self._remain_plan.items()
        }

        self._default_random_seed = 1024

        self._register_events()

    @property
    def frame(self) -> FrameBase:
        return self._frame

    @property
    def snapshots(self) -> SnapshotList:
        return self._snapshots

    def get_agent_idx_list(self) -> List[int]:
        return list(range(len(self._routes)))

    def step(self, tick: int) -> None:
        # Carrier arrive its next destination
        for route_number in self._routes:
            if self._upcoming_arr_time[route_number] is not None and self._upcoming_arr_time[route_number] == tick:
                carrier_arrival_payload = CarrierArrivalPayload(route_number)
                carrier_arrival_event = self._event_buffer.gen_atom_event(
                    tick=tick, event_type=Events.CARRIER_ARRIVAL, payload=carrier_arrival_payload
                )
                self._event_buffer.insert_event(carrier_arrival_event)

        # Update oncall orders
        oncall_orders = self._oncall_order_generator.get_oncall_orders(tick)
        if len(oncall_orders) > 0:
            oncall_receive_payload = OncallReceivePayload(oncall_orders)
            oncall_receive_event = self._event_buffer.gen_atom_event(
                tick=tick, event_type=Events.ONCALL_RECEIVE, payload=oncall_receive_payload
            )
            self._event_buffer.insert_event(oncall_receive_event)

        # Interrupt and throw decision event
        if (tick + 1) % self._config["interrupt_cycle"] == 0 and len(self._oncall_order_buffer) > 0:
            decision_event = self._event_buffer.gen_decision_event(
                tick=tick,
                payload=OncallRoutingPayload(
                    oncall_orders=list(self._oncall_order_buffer)
                )
            )
            self._event_buffer.insert_event(decision_event)

            self._waiting_order_dict = {order.id: order for order in self._oncall_order_buffer}
            self._oncall_order_buffer.clear()

    @property
    def configs(self) -> dict:
        return self._config

    def reset(self, keep_seed: bool = False) -> None:
        new_seed = self._default_random_seed if keep_seed else random[GLOBAL_RAND_KEY].randint(0, 4095)
        random.seed(new_seed)

    def _refresh_arr_time(self, tick: int, route_number: RouteNumber, index: int = 0) -> None:
        plan = self._remain_plan[route_number]
        source_coord = self._carrier_status[route_number].coord if index == 0 else plan[index - 1].order.coord
        target_coord = plan[index].order.coord

        eat = self._eat_predictor.predict(tick, source_coord, target_coord)
        aat = self._aat_predictor.sample(tick, source_coord, target_coord, eat)
        plan[index].act_arr_time = aat
        plan[index].est_arr_time = eat

    def _register_events(self) -> None:
        register_handler = self._event_buffer.register_event_handler

        register_handler(Events.ONCALL_RECEIVE, self._on_oncall_receive)
        register_handler(Events.CARRIER_ARRIVAL, self._on_carrier_arrival)
        register_handler(MaroEvents.TAKE_ACTION, self._on_action_received)

    def _on_oncall_receive(self, event: AtomEvent) -> None:
        payload = event.payload
        assert isinstance(payload, OncallReceivePayload)
        self._oncall_order_buffer.extend(payload.orders)

    def _on_carrier_arrival(self, event: AtomEvent) -> None:
        payload = event.payload
        assert isinstance(payload, CarrierArrivalPayload)

        route_number = payload.route_number
        # TODO: deliver / pickup packages

        plan = self._remain_plan[route_number]
        cur_arrival = plan.pop(0)  # Finish the current plan
        self._carrier_status[route_number].coord = cur_arrival.order.coord
        self._upcoming_arr_time[route_number] = None if len(plan) == 0 else event.tick + plan[0].act_arr_time

    def _on_action_received(self, event: CascadeEvent) -> None:
        actions = event.payload
        assert isinstance(actions, list)

        # Aggregate actions by route
        action_by_route: Dict[RouteNumber, List[Action]] = defaultdict(list)
        for action in actions:
            assert isinstance(action, Action)
            action_by_route[action.route_number].append(action)

        for route_number, actions in action_by_route.items():
            # Sort actions by: 1) insert index, 2) in-segment order
            actions.sort(key=lambda _action: (_action.insert_index, _action.in_segment_order))

            old_plan = self._remain_plan[route_number]
            new_plan = []
            refresh_indexes = []
            j = 0
            for i, old_plan_element in enumerate(old_plan):
                has_new_plan = False
                # Insert all oncall orders that should be inserted before this old stop
                while j < len(actions) and actions[j].insert_index < i:
                    new_order_id = actions[j].order_id
                    new_order = self._waiting_order_dict.pop(new_order_id)  # Remove this order from waiting dict
                    new_plan_element = PlanElement(
                        order=new_order,
                        est_arr_time=-1, act_arr_time=-1  # To be calculated in `self._refresh_arr_time()`
                    )
                    new_plan.append(new_plan_element)
                    refresh_indexes.append(len(new_plan) - 1)
                    j += 1
                    has_new_plan = True

                # Insert this old stop.
                new_plan.append(old_plan_element)

                # If there are new oncall orders before this old stop, refresh predicted time.
                if has_new_plan:
                    refresh_indexes.append(len(new_plan) - 1)

            self._remain_plan[route_number] = new_plan
            for index in refresh_indexes:
                self._refresh_arr_time(tick=event.tick, route_number=route_number, index=index)

        # Put back suspended oncall orders
        self._oncall_order_buffer = deque([order for order in self._waiting_order_dict.values()])
        self._waiting_order_dict.clear()

    def post_step(self, tick: int) -> bool:
        return all(len(plan) == 0 for plan in self._remain_plan.values())


if __name__ == "__main__":
    env = Env(scenario="oncall_routing", topology="example", start_tick=0, durations=10000000)
    is_done = False
    while not is_done:
        _, _event, is_done = env.step(action=None)

        assert isinstance(_event, OncallRoutingPayload)
        if len(_event.oncall_orders) > 0:
            print(env.tick)

            _orders = _event.oncall_orders
            _actions = [
                Action(order_id=_orders[0].id, route_number=987, insert_index=3),
                Action(order_id=_orders[1].id, route_number=987, insert_index=5)
            ]

            env.step(_actions)
            break
