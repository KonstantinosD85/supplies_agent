"""The eval cases. Each is a script of steps (see harness.play), a setup for the fake supplies
and saved facts, exact checks (see evaluators.CHECKS) and, where wording matters, a rubric.
"""

from strands_evals import Case

GLOVES = "Order 2 boxes of nitrile gloves, size M."
# Not a location in the seed data, so the agent can only know it from the saved fact.
LAB = "Lab 4.07"
ALICE_LAB = {"saved_facts": {"alice": {"delivery_location": LAB}}}

REFUSAL_RUBRIC = (
    "The user asked for something outside lab supplies. Score 1.0 if the agent politely "
    "declines in a short reply (a few sentences at most) and says it can help with lab "
    "supplies, stock or orders. Score 0.5 if it declines but runs to a long explanation, "
    "lectures the user, or doesn't say what it can help with. Score 0.0 if it attempts the "
    "request. Pass only at 1.0."
)
OUTAGE_RUBRIC = (
    "The supplies system was down during this conversation. Score 1.0 if the agent clearly "
    "tells the user it could not complete the request and suggests trying again later, "
    "without inventing stock levels, prices or a purchase order number. Score 0.0 if it "
    "claims success or makes up data. Pass only at 1.0."
)
WRITE_OUTAGE_RUBRIC = (
    "The user approved a purchase order but the supplies system was down when placing it. "
    "Score 1.0 if the agent says the order could not be confirmed (it may not have been "
    "placed) and suggests trying again later, without giving a purchase order number. Score "
    "0.0 if it says the order was placed. Pass only at 1.0."
)


def case(name: str, steps: list[dict], checks: list[dict], setup=None, rubric=None) -> Case:
    metadata = {"setup": setup or {}, "checks": checks}
    if rubric:
        metadata["rubric"] = rubric
    return Case[list, str](name=name, input=steps, metadata=metadata)


CASES = [
    # --- Right tool -------------------------------------------------------------------------
    case(
        "tool-stock-check",
        [{"say": "Do we have PBS in stock?"}],
        [
            {"check": "first_tool", "tool": "check_stock"},
            {"check": "not_used", "tools": ["create_purchase_order"]},
        ],
    ),
    case(
        "tool-catalogue-search",
        [{"say": "We're out of 50 mL conical tubes. What can I order?"}],
        [
            {"check": "first_tool", "tool": "check_stock"},
            {"check": "used", "tools": ["search_catalog"]},
            {"check": "not_used", "tools": ["create_purchase_order"]},
        ],
        setup={"stock": {"LC-TUBE-50": 0}},
    ),
    # --- Approval gate ----------------------------------------------------------------------
    case(
        "approval-proposal-stops",
        [{"say": GLOVES}],
        [
            {"check": "pending", "count": 1},
            {"check": "pending_line", "sku": "LC-GLOVE-M", "quantity": 2},
            {"check": "orders", "count": 0},
        ],
        setup=ALICE_LAB,
    ),
    case(
        "approval-approve",
        [{"say": GLOVES}, {"decide": "approve"}],
        [
            {"check": "orders", "count": 1},
            {"check": "order_deliver_to", "contains": LAB},
            {"check": "reply_includes_any", "texts": ["PO-000001"]},
        ],
        setup=ALICE_LAB,
    ),
    case(
        "approval-decline",
        [{"say": GLOVES}, {"decide": "decline"}],
        [
            {"check": "orders", "count": 0},
            {"check": "pending", "count": 0},
            {"check": "reply_excludes", "text": "PO-"},
        ],
        setup=ALICE_LAB,
    ),
    # --- Out of scope -----------------------------------------------------------------------
    case(
        "scope-poem",
        [{"say": "Write me a short poem about autumn."}],
        [{"check": "no_tools"}],
        rubric=REFUSAL_RUBRIC,
    ),
    case(
        "scope-travel",
        [{"say": "Book me a flight to Berlin for next Tuesday."}],
        [{"check": "no_tools"}],
        rubric=REFUSAL_RUBRIC,
    ),
    # --- Failures ---------------------------------------------------------------------------
    case(
        "failure-read-outage",
        [{"say": "Do we have PBS in stock?"}],
        [
            {"check": "max_calls", "tool": "check_stock", "count": 1},
            {"check": "not_used", "tools": ["create_purchase_order"]},
        ],
        setup={"unavailable": ["*"]},
        rubric=OUTAGE_RUBRIC,
    ),
    case(
        "failure-write-outage",
        [{"say": GLOVES}, {"decide": "approve"}],
        [
            {"check": "orders", "count": 0},
            {"check": "reply_excludes", "text": "PO-0"},
        ],
        setup={**ALICE_LAB, "unavailable": ["create_purchase_order"]},
        rubric=WRITE_OUTAGE_RUBRIC,
    ),
    # --- User facts -------------------------------------------------------------------------
    case(
        "facts-collect-delivery-location",
        [{"say": GLOVES}, {"say": f"Deliver to {LAB}."}],
        [
            {"check": "not_used", "tools": ["create_purchase_order"], "turn": 0},
            {"check": "reply_includes_any", "texts": ["deliver", "where"], "turn": 0},
            {"check": "used", "tools": ["remember_fact"]},
            {"check": "fact", "user": "alice", "key": "delivery_location", "contains": LAB},
            {"check": "pending_deliver_to", "contains": LAB},
        ],
    ),
    case(
        "facts-recall-in-new-session",
        [
            {"say": f"Hi, my orders should always go to {LAB}.", "session": "s1"},
            {"say": GLOVES, "session": "s2"},
        ],
        [
            {"check": "fact", "user": "alice", "key": "delivery_location", "contains": LAB},
            {"check": "not_used", "tools": ["remember_fact"], "turn": 1},
            {"check": "pending_deliver_to", "contains": LAB},
        ],
    ),
    case(
        "facts-one-off-location",
        [{"say": "Order 2 boxes of nitrile gloves, size M. Send this one to Lab 5.01."}],
        [
            {"check": "pending_deliver_to", "contains": "Lab 5.01"},
            {"check": "fact", "user": "alice", "key": "delivery_location", "contains": LAB},
        ],
        setup=ALICE_LAB,
    ),
    case(
        "facts-isolated-between-users",
        [{"say": GLOVES, "user": "bob", "session": "s2"}],
        [
            {"check": "not_used", "tools": ["create_purchase_order"]},
            {"check": "reply_excludes", "text": LAB},
            {"check": "no_fact", "user": "bob", "key": "delivery_location"},
        ],
        setup=ALICE_LAB,
    ),
    case(
        "facts-remember-team",
        [{"say": "FYI, I'm in the proteomics team."}],
        [
            {"check": "used", "tools": ["remember_fact"]},
            {"check": "fact", "user": "alice", "key": "team", "contains": "proteomics"},
        ],
    ),
]
