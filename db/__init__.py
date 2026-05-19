"""bot_data.db package — schema + async CRUD."""
from .operations import (
    add_bol_to_cache,
    add_pod_to_cache,
    all_bols_accepted,
    can_accept_pod,
    clear_all_loads_for_group,
    clear_load_from_cache,
    get_bols_count,
    get_company_permissions,
    get_delivery_count,
    get_last_bol,
    get_load_from_cache,
    get_pickup_count,
    get_pods_count,
    has_bol_for_load,
    has_pods_for_load,
    init_load_in_cache,
    is_bol_accepted,
    needs_more_bols,
    remove_last_bol_for_load,
    remove_last_pod_for_load,
    remove_team_driver_db,
    save_driver_id_db,
    save_team_driver_id_db,
    set_bol_accepted,
    set_last_bol_accepted,
)
from .schema import init_db

__all__ = [
    "init_db",
    # loads
    "init_load_in_cache",
    "get_pickup_count",
    "get_delivery_count",
    "get_load_from_cache",
    "clear_load_from_cache",
    "clear_all_loads_for_group",
    # bols
    "add_bol_to_cache",
    "get_bols_count",
    "get_last_bol",
    "all_bols_accepted",
    "is_bol_accepted",
    "set_last_bol_accepted",
    "set_bol_accepted",
    "needs_more_bols",
    "has_bol_for_load",
    "remove_last_bol_for_load",
    # pods
    "add_pod_to_cache",
    "get_pods_count",
    "can_accept_pod",
    "has_pods_for_load",
    "remove_last_pod_for_load",
    # groups
    "save_driver_id_db",
    "save_team_driver_id_db",
    "remove_team_driver_db",
    # company_permissions
    "get_company_permissions",
]
