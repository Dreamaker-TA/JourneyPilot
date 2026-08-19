from enum import Enum


class ResearchDomain(str, Enum):
    VISIT = "visit"
    DINING = "dining"
    LODGING = "lodging"
    LOCAL_TRANSPORT = "local_transport"
    LONG_DISTANCE_TRANSPORT = "long_distance_transport"
