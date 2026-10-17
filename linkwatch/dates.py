from dateutil import parser


def parse_last_modified(value):
    if not value:
        return None
    try:
        return parser.parse(value)
    except (ValueError, OverflowError):
        return None
