# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import List, Optional

from maro.simulator import Env
from maro.simulator.scenarios.oncall_routing import Coordinate
from maro.simulator.scenarios.oncall_routing.common import Action, OncallRoutingPayload
from maro.utils import set_seeds

set_seeds(0)


def _is_equal_segment(action1: Action, action2: Action) -> bool:
    return (action1.route_name, action1.insert_index) == (action2.route_name, action2.insert_index)


def _get_actions(running_env: Env, event: OncallRoutingPayload) -> List[Action]:
    tick = running_env.tick
    oncall_orders = event.oncall_orders
    route_meta_info_dict = event.route_meta_info_dict
    route_plan_dict = event.route_plan_dict
    carriers_in_stop: List[bool] = (running_env.snapshot_list["carriers"][tick::"in_stop"] == 1).tolist()
    est_duration_predictor = event.estimated_duration_predictor

    route_original_indexes = {}
    for route_name in route_meta_info_dict:
        num_order = len(route_plan_dict[route_name])
        route_original_indexes[route_name] = list(range(num_order))

    actions = []
    for oncall_order in oncall_orders:
        # Best result without violating any time windows
        min_distance_violate = float("inf")
        chosen_route_name_violate: Optional[str] = None
        insert_idx_violate = -1
        # Best result with violating time windows
        min_distance_no_violate = float("inf")
        chosen_route_name_no_violate: Optional[str] = None
        insert_idx_no_violate = -1

        for route_name, meta in route_meta_info_dict.items():
            carrier_idx = meta["carrier_idx"]
            estimated_next_departure_tick: int = meta["estimated_next_departure_tick"]
            planned_orders = route_plan_dict[route_name]

            for i, planned_order in enumerate(planned_orders):
                if i == 0 and not carriers_in_stop[carrier_idx]:
                    continue
                distance = est_duration_predictor.predict(tick, oncall_order.coord, planned_order.coord)

                # Check if it will break any violate any time window
                is_time_valid = True
                cur_tick = tick
                for j in range(len(planned_orders)):  # Simulate all orders
                    if j == i:  # If we need to insert the oncall order before the j'th planned order
                        if j == 0:  # Carrier in stop. Insert before the first stop.
                            current_staying_stop_coordinate: Coordinate = meta["current_staying_stop_coordinate"]
                            cur_tick += est_duration_predictor.predict(  # Current stop => oncall order
                                cur_tick, current_staying_stop_coordinate, oncall_order.coord)
                            cur_tick += estimated_next_departure_tick
                        else:
                            cur_tick += est_duration_predictor.predict(  # Last planned order => oncall order
                                cur_tick, planned_orders[j - 1].coord, oncall_order.coord
                            )
                        # Violate oncall order time window
                        if not oncall_order.open_time <= cur_tick <= oncall_order.close_time:
                            is_time_valid = False
                            break

                        cur_tick += est_duration_predictor.predict(  # Oncall order => current planned order
                            cur_tick, oncall_order.coord, planned_orders[j].coord
                        )
                    else:
                        if j == 0:
                            estimated_duration_to_the_next_stop: int = meta["estimated_duration_to_the_next_stop"]
                            if carriers_in_stop[carrier_idx]:  # Current stop => first planned order
                                cur_tick += estimated_duration_to_the_next_stop
                                cur_tick += estimated_next_departure_tick
                            else:
                                cur_tick += estimated_duration_to_the_next_stop
                        else:
                            cur_tick += est_duration_predictor.predict(  # Last planned order => current planned order
                                cur_tick, planned_orders[j - 1].coord, planned_orders[j].coord
                            )
                    # Violate current planned order time window
                    if not planned_orders[j].open_time <= cur_tick <= planned_orders[j].close_time:
                        is_time_valid = False
                        break

                if is_time_valid:
                    if distance < min_distance_no_violate:
                        min_distance_no_violate = distance
                        chosen_route_name_no_violate = route_name
                        insert_idx_no_violate = i
                else:
                    if distance < min_distance_violate:
                        min_distance_violate = distance
                        chosen_route_name_violate = route_name
                        insert_idx_violate = i

        if chosen_route_name_no_violate is not None:
            actions.append(Action(
                order_id=oncall_order.id,
                route_name=chosen_route_name_no_violate,
                insert_index=route_original_indexes[chosen_route_name_no_violate][insert_idx_no_violate]
            ))
            route_plan_dict[chosen_route_name_no_violate].insert(insert_idx_no_violate, oncall_order)
            route_original_indexes[chosen_route_name_no_violate].insert(
                insert_idx_no_violate, route_original_indexes[chosen_route_name_no_violate][insert_idx_no_violate]
            )
        elif chosen_route_name_violate is not None:
            actions.append(Action(
                order_id=oncall_order.id,
                route_name=chosen_route_name_violate,
                insert_index=route_original_indexes[chosen_route_name_violate][insert_idx_violate]
            ))
            route_plan_dict[chosen_route_name_violate].insert(insert_idx_violate, oncall_order)
            route_original_indexes[chosen_route_name_violate].insert(
                insert_idx_violate, route_original_indexes[chosen_route_name_violate][insert_idx_violate]
            )

    # Add segment index if multiple orders are share
    actions.sort(key=lambda action: (action.route_name, action.insert_index))
    segment_index = 0
    for idx in range(len(actions) - 1):
        if _is_equal_segment(actions[idx], actions[idx + 1]):
            segment_index += 1
            actions[idx + 1].in_segment_order = segment_index
        else:
            segment_index = 0

    return actions


# Greedy: assign each on-call order to the closest stop on existing route.
if __name__ == "__main__":
    env = Env(
        scenario="oncall_routing", topology="example", start_tick=0, durations=1440,
    )

    env.reset(keep_seed=True)
    metrics, decision_event, is_done = env.step(None)
    while not is_done:
        assert isinstance(decision_event, OncallRoutingPayload)
        print(f"Processing {len(decision_event.oncall_orders)} oncall orders at tick {env.tick}.")
        metrics, decision_event, is_done = env.step(_get_actions(env, decision_event))

    print(metrics)
