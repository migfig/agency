from enum import Enum


class NodeType(str, Enum):
    AGENT = "agent"
    CONDITIONAL = "conditional"
    MERGE = "merge"
    BROADCAST = "broadcast"
    HUMAN_IN_LOOP = "human_in_loop"
    TOOL_CALL = "tool_call"
