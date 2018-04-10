"""Schema definition for analyzer results."""

from voluptuous import Required
from voluptuous import Schema

from thoth.common import parse_datetime


class Datetime(object):
    """Check datetime fields against ISO format."""

    def __call__(self, dt):
        return parse_datetime(dt)


# Describe Python version used by the analyzer image.
PYTHON_SCHEMA = Schema({
    Required("api_version"): int,
    Required("implementation_name"): str,   # e.g. cpython
    Required("major"): int,
    Required("micro"): int,
    Required("minor"): int,
    Required("releaselevel"): str,
    Required("serial"): int
})


# Describe Linux distribution details that was run in the analyzer image.
DISTRIBUTION_SCHEMA = Schema({
    Required("codename"): str,  # e.g. "Twenty Seven"
    Required("id"): str,  # e.g. "fedora"
    Required("like"): str,
    Required("version"): str,
    Required("version_parts"): {
        Required("build_number"): str,
        Required("major"): str,
        Required("minor"): str
    }
})


# Metadata about results produced by analyzers.
METADATA_SCHEMA = Schema({
    Required("analyzer"): str,
    Required("analyzer_version"): str,
    Required("arguments"): dict,
    Required("datetime"): Datetime(),
    Required("distribution"): DISTRIBUTION_SCHEMA,
    Required("hostname"): str,
    Required("python"): PYTHON_SCHEMA
})


# Top level schema for analyzer results.
RESULT_SCHEMA = Schema({
    Required("metadata"): METADATA_SCHEMA,
    Required("result"): object
})
