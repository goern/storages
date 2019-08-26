#!/usr/bin/env python3
# thoth-storages
# Copyright(C) 2019 Francesco Murdaca, Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""A Dgraph server adapter communicating via gRPC."""

import logging
import os
import json
import pkg_resources
import functools
from typing import List
from typing import Set
from typing import Tuple
from typing import Optional
from typing import Dict
from typing import Iterable
from typing import Union
from pathlib import Path
from dateutil import parser
from datetime import timezone
from itertools import chain
from collections import deque
import asyncio
from math import nan

import attr
import grpc
import pydgraph

from thoth.common import OpenShift
from thoth.common import RuntimeEnvironment as RuntimeEnvironmentConfig
from thoth.common import HardwareInformation as HardwareInformationConfig
from thoth.python import Pipfile
from thoth.python import PipfileLock
from thoth.python import PackageVersion

from ..base import StorageBase
from .cache import GraphCache
from .models_base import enable_vertex_cache
from .models_base import VertexBase
from .models import ALL_MODELS
from .models import AdviserRun
from .models import AdvisedSoftwareStack
from .models import AdviserRunSoftwareEnvironmentInput
from .models import AdviserStackInput
from .models import Advised
from .models import AnalyzedBy
from .models import BuildSoftwareEnvironment as BuildSoftwareEnvironmentModel
from .models import CreatesStack
from .models import CVE
from .models import DebDepends
from .models import DebDependency
from .models import DebPackageVersion
from .models import DebPreDepends
from .models import DebReplaces
from .models import DependencyMonkeyRun
from .models import DependencyMonkeyRunSoftwareEnvironmentInput
from .models import DependsOn
from .models import EcosystemSolverRun
from .models import EnvironmentBase
from .models import HardwareInformation as HardwareInformationModel
from .models import UserHardwareInformation as UserHardwareInformationModel
from .models import HasArtifact
from .models import Investigated
from .models import HasVulnerability
from .models import Identified
from .models import InspectionBuildSoftwareEnvironmentInput
from .models import InspectionRun
from .models import InspectionRunSoftwareEnvironmentInput
from .models import InspectionSoftwareStack
from .models import InspectionStackInput
from .models import InstalledFrom
from .models import PackageExtractRun
from .models import PackageAnalyzerRun
from .models import PythonFileDigest
from .models import FoundFile
from .models import PackageAnalyzerInput
from .models import IncludedFile
from .models import ProvenanceCheckerRun
from .models import ProvenanceCheckerStackInput
from .models import ProvidedBy
from .models import PythonArtifact
from .models import PythonPackageIndex
from .models import PythonPackageRequirement
from .models import PythonPackageVersion
from .models import PythonPackageVersionEntity
from .models import RequirementsInput
from .models import Requires
from .models import Resolved
from .models import RPMPackageVersion
from .models import RPMRequirement
from .models import RunSoftwareEnvironment as RunSoftwareEnvironmentModel
from .models import UserRunSoftwareEnvironment as UserRunSoftwareEnvironmentModel
from .models import Solved
from .models import SoftwareStackBase
from .models import UsedIn
from .models import UsedInBuild
from .models import UsedInJob
from .models import UserSoftwareStack
from .performance import ObservedPerformance, PerformanceIndicatorBase
from .performance import PERFORMANCE_MODEL_BY_NAME, ALL_PERFORMANCE_MODELS

from ..exceptions import NotFoundError
from ..exceptions import PythonIndexNotRegistered
from ..exceptions import PerformanceIndicatorNotRegistered
from ..exceptions import NotConnected
from ..advisers import AdvisersResultsStore
from ..analyses import AnalysisResultsStore
from ..package_analyses import PackageAnalysisResultsStore
from ..inspections import InspectionResultsStore
from ..provenance import ProvenanceResultsStore
from ..dependency_monkey_reports import DependencyMonkeyReportsStore
from ..solvers import SolverResultsStore

_LOGGER = logging.getLogger(__name__)


@attr.s(slots=True)
class GraphDatabase(StorageBase):
    """A dgraph server adapter communicating via gRPC."""

    TLS_PATH = os.getenv("GRAPH_TLS_PATH")
    ENVVAR_HOST_NAME = "GRAPH_SERVICE_HOST"
    DEFAULT_HOSTS = list((os.getenv(ENVVAR_HOST_NAME) or "localhost:9080").split(","))

    tls_path = attr.ib(type=str, default=TLS_PATH, kw_only=True)
    hosts = attr.ib(type=list, default=DEFAULT_HOSTS, kw_only=True)
    cache = attr.ib(type=GraphCache, default=attr.Factory(GraphCache.load), kw_only=True)

    _client = attr.ib(type=pydgraph.DgraphClient, default=None, kw_only=True)
    _stubs = attr.ib(type=list, default=attr.Factory(list), kw_only=True)

    @property
    def client(self) -> pydgraph.DgraphClient:
        """Retrieve client for communicating with DGraph instance."""
        if self._client is None:
            raise NotConnected("No client established to talk to a Draph instance")

        return self._client

    def __del__(self) -> None:
        """Disconnect properly on object destruction."""
        if self.is_connected():
            self.disconnect()

    @classmethod
    def create(cls, host: str):
        """Create a graph adapter, only for one host (syntax sugar)."""
        return cls(hosts=[host])

    def is_connected(self) -> bool:
        """Check if we are connected to a remote Dgraph instance."""
        return self._client is not None

    def connect(self):
        """Connect to a Dgraph via gRPC."""
        credentials = None
        if self.tls_path:
            root_ca_cert = (Path(self.tls_path) / "./ca.crt").read_bytes()
            client_cert_key = (Path(self.tls_path) / "./client.user.key").read_bytes()
            client_cert = (Path(self.tls_path) / "./client.user.crt").read_bytes()

            credentials = grpc.ssl_channel_credentials(
                root_certificates=root_ca_cert, private_key=client_cert_key, certificate_chain=client_cert
            )

        for address in self.hosts:
            self._stubs.append(pydgraph.DgraphClientStub(address, credentials=credentials))

        self._client = pydgraph.DgraphClient(*self._stubs)

    def disconnect(self):
        """Close all connections - disconnect from remote."""
        for stub in self._stubs:
            stub.close()

        self._stubs = []
        del self._client
        self._client = None

    def initialize_schema(self) -> None:
        """Initialize Dgraph's schema."""
        try:
            version_self = pkg_resources.get_distribution("thoth-storages").version
        except pkg_resources.DistributionNotFound:
            version_self = "UNKNOWN"

        _LOGGER.info("Initializing Dgraph with schema, schema version is %r", version_self)
        schema = (Path(__file__).parent / "schema.rdf").read_text()
        operation = pydgraph.Operation(schema=schema)
        self.client.alter(operation)

    def drop_all(self) -> None:
        """Drop all data present inside Dgraph instance."""
        _LOGGER.warning("Dropping all data on Dgraph's instance")
        operation = pydgraph.Operation(drop_all=True)
        self.client.alter(operation)

    @staticmethod
    def normalize_python_package_name(package_name: str) -> str:
        """Normalize Python package name based on PEP-0503."""
        return PackageVersion.normalize_python_package_name(package_name)

    @staticmethod
    def parse_python_solver_name(solver_name: str) -> dict:
        """Parse os and Python identifiers encoded into solver name."""
        if solver_name.startswith("solver-"):
            solver_identifiers = solver_name[len("solver-"):]
        else:
            raise ValueError(f"Solver name has to start with 'solver-' prefix: {solver_name!r}")

        parts = solver_identifiers.split("-")
        if len(parts) != 3:
            raise ValueError(
                "Solver should be in a form of 'solver-<os_name>-<os_version>-<python_version>, "
                f"solver name {solver_name} does not correspond to this naming schema"
            )

        python_version = parts[2]
        if python_version.startswith("py"):
            python_version = python_version[len("py"):]
        else:
            raise ValueError(
                f"Python version encoded into Python solver name does not start with 'py' prefix: {solver_name}"
            )

        python_version = ".".join(list(python_version))
        return {"os_name": parts[0], "os_version": parts[1], "python_version": python_version}

    def _query_raw(self, query: str, variables: dict = None, *, read_only: bool = True) -> dict:
        """Perform raw query on connected Dgraph instance."""
        assert self._client is not None, "Adapter is not connected to any Dgraph instance."
        txn = self._client.txn(read_only=read_only)

        if bool(int(os.getenv("THOTH_STORAGES_DEBUG_QUERIES", 0))):
            _LOGGER.debug("Performing query with variables (read_only=%r): %r\n%s", read_only, variables, query)

        try:
            result = txn.query(query, variables=variables)
        finally:
            # Safe after commit based on docs.
            txn.discard()

        if bool(int(os.getenv("THOTH_STORAGES_DEBUG_QUERIES", 0))):
            _LOGGER.debug("Query statistics:\n%s", result.latency)

        return json.loads(result.json)

    async def _query_raw_async(self, query) -> dict:
        """An async wrapper for a query call."""
        return self._query_raw(query)

    def _query_raw_concurrent(self, queries: List[str]) -> List[dict]:
        """Execute multiple queries in concurrent mode."""
        if len(queries) == 0:
            return []

        tasks = []
        for query in queries:
            task = asyncio.ensure_future(self._query_raw_async(query))
            tasks.append(task)

        loop = asyncio.get_event_loop()
        results = loop.run_until_complete(asyncio.gather(*tasks))
        return list(chain(results))

    def get_analysis_metadata(self, analysis_document_id: str) -> dict:
        """Get metadata stored for the given analysis document."""
        query = """
        {
            f(func: has(%s)) @filter(eq(analysis_document_id, %s)) {
                analysis_datetime
                analysis_document_id
                package_extract_name
                package_extract_version
            }
        }
        """ % (
            PackageExtractRun.get_label(),
            analysis_document_id,
        )
        result = self._query_raw(query)
        if not result:
            raise NotFoundError(f"Analysis with analysis document if {analysis_document_id} was not found")

        result["f"][0]["analysis_datetime"] = parser.parse(result["f"][0]["analysis_datetime"]).replace(
            tzinfo=timezone.utc
        )

        return result["f"][0]

    def run_software_environment_listing(
        self, start_offset: int = 0, count: int = 100, is_user_run: bool = False
    ) -> list:
        """Get listing of software environments available for run."""
        run_software_environment = RunSoftwareEnvironmentModel.get_label()
        if is_user_run:
            run_software_environment = UserRunSoftwareEnvironmentModel.get_label()
        query = """
        {
            f(func: has(%s), first: %d, offset: %d) {
                e: environment_name
            }
        }
        """ % (
            run_software_environment,
            count,
            start_offset,
        )
        result = self._query_raw(query)
        return list(chain(item["e"] for item in result["f"]))

    def build_software_environment_listing(self, start_offset: int = 0, count: int = 100) -> list:
        """Get listing of software environments available for build."""
        query = """
        {
            f(func: has(%s), first: %d, offset: %d) {
                e: environment_name
            }
        }
        """ % (
            BuildSoftwareEnvironmentModel.get_label(),
            count,
            start_offset,
        )
        result = self._query_raw(query)
        return list(chain(item["e"] for item in result["f"]))

    @staticmethod
    def _postprocess_environment_analysis_listing(
        query_result: dict, environment_name: str, convert_datetime: bool
    ) -> list:
        """Post-process build/run software environment analysis listing query."""
        if query_result["f"][0]["count"] == 0:
            raise NotFoundError(f"No analyses found for environment {environment_name!r}")
        if convert_datetime:
            for entry in query_result["f"][0].get("analyzed_by", []):
                entry["analysis_datetime"] = parser.parse(entry["analysis_datetime"]).replace(tzinfo=timezone.utc)

        return [analysis for analysis in query_result["f"][0].get("analyzed_by", [])]

    def run_software_environment_analyses_listing(
        self,
        run_software_environment_name: str,
        start_offset: int = 0,
        count: int = 100,
        convert_datetime: bool = True,
        is_user_run: bool = False,
    ) -> list:
        """Get listing of analyses available for the given software environment for run."""
        run_software_environment = RunSoftwareEnvironmentModel.get_label()
        if is_user_run:
            run_software_environment = UserRunSoftwareEnvironmentModel.get_label()
        query = """
        {
            f(func: has(%s), first: %d, offset: %d) @filter(eq(environment_name,"%s")){
                count:count(environment_name)
                analyzed_by {
                    analysis_datetime
                    analysis_document_id
                    package_extract_name
                    package_extract_version
                }
            }
        }
        """ % (
            run_software_environment,
            count,
            start_offset,
            run_software_environment_name,
        )
        result = self._query_raw(query)
        if result["f"]:
            return self._postprocess_environment_analysis_listing(
                result, run_software_environment_name, convert_datetime
            )
        else:
            return []

    def build_software_environment_analyses_listing(
        self,
        build_software_environment_name: str,
        start_offset: int = 0,
        count: int = 100,
        convert_datetime: bool = True,
    ) -> list:
        """Get listing of analyses available for the given software environment for build."""
        query = """
        {
            f(func: has(%s), first: %d, offset: %d) @filter(eq(environment_name,"%s")){
                count:count(environment_name)
                analyzed_by {
                    analysis_datetime
                    analysis_document_id
                    package_extract_name
                    package_extract_version
                }
            }
        }
        """ % (
            BuildSoftwareEnvironmentModel.get_label(),
            count,
            start_offset,
            build_software_environment_name,
        )
        result = self._query_raw(query)
        if result["f"]:
            return self._postprocess_environment_analysis_listing(
                result, build_software_environment_name, convert_datetime
            )
        else:
            return []

    def python_package_version_exists(
            self,
            package_name: str,
            package_version: str,
            index_url: str = None,
            solver_name: str = None
    ) -> bool:
        """Check if the given Python package version exists in the graph database.

        If optional solver_name parameter is set, the call answers if the given package was solved by
        the given solver. Otherwise, any solver run is taken into account.
        """
        package_name = self.normalize_python_package_name(package_name)

        q = ""
        if index_url:
            q = q + ' AND eq(index_url, "%s")' % index_url

        if solver_name:
            query = """
            {
                f(func: has(%s)) @filter(eq(solver_name, "%s")) @cascade @normalize {
                    solved @filter(eq(package_name, "%s") AND eq(package_version, "%s") AND eq(ecosystem, python)%s) {
                        count: count(~solved)
                    }
                }
            }
            """ % (
                EcosystemSolverRun.get_label(),
                solver_name,
                package_name,
                package_version,
                q,
            )
        else:
            query = """{
                f(func: has(%s)) @filter(eq(package_name, "%s") AND eq(package_version, "%s")
                AND eq(ecosystem, python)%s) {
                    count(uid)
                }
            }
            """ % (
                PythonPackageVersionEntity.get_label(),
                package_name,
                package_version,
                q,
            )

        result = self._query_raw(query)
        if not result["f"]:
            return False

        return result["f"][0].get("count", 0) > 0

    def python_package_exists(self, package_name: str) -> bool:
        """Check if the given Python package exists regardless of version."""
        package_name = self.normalize_python_package_name(package_name)
        query = """{
            f(func: has(%s)) @filter(eq(package_name, "%s")) {
                count(uid)
            }
        }
        """ % (
            PythonPackageVersionEntity.get_label(),
            package_name,
        )
        result = self._query_raw(query)

        return result["f"][0]["count"] > 0

    @staticmethod
    def _construct_filter_eq_from_dict(dict_) -> str:
        """Construct a filter query from a dict matching all the properties."""
        filter_query = ""
        for key, value in dict_.items():
            if filter_query:
                filter_query += " AND "
            if isinstance(value, str):
                filter_query += f'eq({key}, "{value}")'
            else:
                filter_query += f"eq({key}, {value})"
        return filter_query

    def compute_python_package_version_avg_performance(
        self, packages: Set[tuple], *, run_software_environment: dict = None, hardware_specs: dict = None
    ) -> float:
        """Get average performance of Python packages on the given runtime environment.

        We derive this average performance based on software stacks we have
        evaluated on the given software environment for run including the given
        package in specified version. There are also included stacks that
        failed for some reason that have negative performance impact on the overall value.

        There are considered software stacks that include packages listed,
        they can however include also other packages.

        Optional parameters additionally slice results - e.g. if run_software_environment is set,
        it picks only results that match the given parameters criteria.
        """
        if not packages:
            raise ValueError("No packages provided for the query")

        # Create a list so we can index packages in log messages, also normalize names according to PEP.
        normalized_packages = []
        for package_tuple in packages:
            normalized_packages.append(
                (self.normalize_python_package_name(package_tuple[0]), package_tuple[1], package_tuple[2])
            )

        queries = []
        for idx, package_tuple in enumerate(normalized_packages):
            package_name, package_version, index_url = package_tuple
            run_software_env_filter = ""
            if run_software_environment:
                run_software_env_filter = "~inspection_software_environment_input @filter("
                run_software_env_filter += self._construct_filter_eq_from_dict(run_software_environment)
                run_software_env_filter += ") { uid }"

            hw_filter = ""
            if hardware_specs:
                hw_filter = "~used_in_job @filter("
                hw_filter += self._construct_filter_eq_from_dict(hardware_specs)
                hw_filter += ") { uid }"

            query = """
            {
                q(func: has(%s)) @cascade @normalize {
                    uid: uid
                    ~inspection_stack_input {
                        ~creates_stack @filter(eq(package_name, "%s") """ \
            """AND eq(package_version, "%s") AND eq(index_url, "%s")) {
                            package_name
                        }
                    }
                    %s
                    %s
                }
            }
            """ % (
                InspectionRun.get_label(),
                package_name,
                package_version,
                index_url,
                hw_filter,
                run_software_env_filter,
            )
            queries.append(query)

        results = self._query_raw_concurrent(queries)

        all_uids = []
        for idx, item in enumerate(results):
            if not item["q"]:
                # No stack was found that would include the given package, return None directly.
                _LOGGER.debug("No stack was found for package %r", normalized_packages[idx])
                return nan

            uids = []
            for uid in item["q"]:
                uids.append(uid["uid"])

            all_uids.append(set(uids))

        all_stacks = set.intersection(*all_uids)
        if not all_stacks:
            # No intersection was found - no stacks which would include all the packages specified found.
            return nan

        # Now retrieve average performance for each and every micro-benchmark of a performance type.
        queries = []
        for inspection_stack_id in all_stacks:
            # TODO: add performance micro-benchmark type as a parameter
            query = (
                """
            {
                q(func: uid(%s)) @normalize {
                    observed_performance {
                        p: overall_score
                    }
                }
            }
            """
                % inspection_stack_id
            )
            queries.append(query)

        results = self._query_raw_concurrent(queries)
        overall_score = 0.0
        count = 0
        for result in results:
            for performance_indicator_record in result["q"]:
                overall_score += performance_indicator_record["p"]
                count += 1

        if count == 0:
            # No performance indicators found
            return nan
        else:
            return overall_score / count

    def has_python_solver_error(
        self,
        package_name: str,
        package_version: str,
        index_url: str,
        *,
        os_name: str,
        os_version: str,
        python_version: str,
    ) -> bool:
        """Retrieve information whether the given package has any solver error."""
        run_software_env = ""
        if os_name:
            run_software_env = f' AND eq(os_name, "{os_name}")'

        if os_version:
            run_software_env += f' AND eq(os_version, "{os_version}")'

        if python_version:
            run_software_env += f' AND eq(python_version, "{python_version}")'

        query = """
        {
            f(func: has(%s)) @filter(eq(package_name, "%s") AND eq(package_version, "%s") AND eq(index_url, "%s")%s) {
                e: solver_error
            }
        }
        """ % (
            PythonPackageVersion.get_label(),
            package_name,
            package_version,
            index_url,
            run_software_env,
        )
        result = self._query_raw(query)
        if not result["f"]:
            raise NotFoundError(
                f"Package {package_name!r} in version {package_version!r} from index {index_url!r} not found for "
                f"operating system '{os_name}:{os_version}', python version: {python_version!r}"
            )

        return any(i["e"] for i in result["f"])

    def get_all_versions_python_package(
        self,
        package_name: str,
        index_url: str = None,
        *,
        only_known_index: bool = False,
        os_name: str = None,
        os_version: str = None,
        python_version: str = None,
        without_error: bool = True,
        only_solved: bool = False,
    ) -> List[Tuple[str, str]]:
        """Get all versions available for a Python package."""
        package_name = self.normalize_python_package_name(package_name)

        q = ""
        if os_name:
            q = q + ' AND eq(os_name, "%s")' % os_name

        if os_version:
            q = q + ' AND eq(os_version, "%s")' % os_version

        if python_version:
            q = q + ' AND eq(python_version, "%s")' % python_version

        if without_error:
            q = q + " AND eq(solver_error, false)"
        else:
            q = q + " AND eq(solver_error, true)"

        if index_url:
            q = q + ' AND eq(index_url, "%s")' % index_url

        if only_solved:
            q = q + " AND has(~solved)"

        query = """
            {
                f(func: has(%s)) @filter(eq(package_name, "%s")%s) %s {
                    package_version
                    index_url
                }
            }
            """ % (
            PythonPackageVersion.get_label(),
            package_name,
            q,
            "@cascade" if only_known_index else ""
        )

        result = self._query_raw(query)

        return [tuple(python_package.values()) for python_package in result["f"]]

    @staticmethod
    def _postprocess_retrieve_packages(result: dict) -> dict:
        """Post-process retrieve packages query."""
        pp_result = {}
        for package in result["f"]:
            if package["package_name"] in pp_result.keys():
                if package["package_version"] in pp_result[package["package_name"]]:
                    pass
                else:
                    pp_result[package["package_name"]] = pp_result[package["package_name"]] + [
                        package["package_version"]
                    ]
            else:
                pp_result[package["package_name"]] = [package["package_version"]]

        return pp_result

    def retrieve_unsolved_python_packages(self, solver_name: str) -> dict:
        """Retrieve a dictionary mapping package names to versions that dependencies were not yet resolved.

        Using solver_name argument the query narrows down to packages that were not resolved by the given solver.
        """
        solver_info = self.parse_python_solver_name(solver_name)
        query = """
        {
          pve as var(func: has(%s)) {
            cnt as count(installed_from @filter(eq(os_name, "%s") """\
                """AND eq(os_version, "%s") AND eq(python_version, "%s")))
          }

          f(func: uid(pve)) @filter(eq(val(cnt), 0)) {
            package_name
            package_version
          }
        }
        """ % (
            PythonPackageVersionEntity.get_label(),
            solver_info["os_name"],
            solver_info["os_version"],
            solver_info["python_version"],
        )
        result = self._query_raw(query)

        return self._postprocess_retrieve_packages(result)

    def retrieve_unsolved_python_packages_count(self, solver_name: str) -> int:
        """Retrieve number of unsolved Python packages for the given solver."""
        solver_info = self.parse_python_solver_name(solver_name)
        query = """
        {
          pve as var(func: has(%s)) {
            cnt as count(installed_from @filter(eq(os_name, "%s") """\
                """AND eq(os_version, "%s") AND eq(python_version, "%s")))
          }

          f(func: uid(pve)) @filter(eq(val(cnt), 0)) {
            uid
          }
        }
        """ % (
            PythonPackageVersionEntity.get_label(),
            solver_info["os_name"],
            solver_info["os_version"],
            solver_info["python_version"],
        )
        result = self._query_raw(query)

        return len(result["f"])

    def retrieve_solved_python_packages_count(self, solver_name: str) -> int:
        """Retrieve number of solved Python packages for the given solver."""
        solver_info = self.parse_python_solver_name(solver_name)
        query = """
        {
          pve as var(func: has(%s)) {
            cnt as count(installed_from @filter(eq(os_name, "%s") """\
                """AND eq(os_version, "%s") AND eq(python_version, "%s")))
          }

          f(func: uid(pve)) @filter(gt(val(cnt), 0)) {
            uid
          }
        }
        """ % (
            PythonPackageVersionEntity.get_label(),
            solver_info["os_name"],
            solver_info["os_version"],
            solver_info["python_version"],
        )
        result = self._query_raw(query)

        return len(result["f"])

    def retrieve_unanalyzed_python_package_versions(self, start_offset: int = 0, count: int = 100) -> List[dict]:
        """Retrieve a list of package names, versions and index urls that were not analyzed yet by package-analyzer."""
        query = """{
            f(func: has(%s), first: %d, offset: %d) @filter(NOT has(%s)) {
                package_name
                package_version
                index_url
            }
        }""" % (
            PythonPackageVersionEntity.get_label(),
            count,
            start_offset,
            PackageAnalyzerInput.get_name(),
        )
        result = self._query_raw(query)

        return result["f"]

    def retrieve_solved_python_packages(self, count: int = 10, start_offset: int = 0, solver_name: str = None) -> dict:
        """Retrieve a dictionary mapping package names to versions for dependencies that were already solved.

        Using count and start_offset is possible to change pagination.
        Using solver_name argument the query narrows down to packages that were resolved by the given solver.
        """
        q = ""
        if solver_name:
            solver_info = self.parse_python_solver_name(solver_name)
            q += "@filter("
            q = q + 'eq(os_name, "%s")' % solver_info["os_name"]
            q = q + ' AND eq(os_version, "%s")' % solver_info["os_version"]
            q = q + ' AND eq(python_version, "%s")' % solver_info["python_version"]
            q += ")"

        query = """{
           f(func: has(%s), first: %d, offset: %d) %s @normalize {
               %s {
                   package_name:package_name
                   package_version:package_version
                    }
                }
            }""" % (
            Solved.get_name(),
            count,
            start_offset,
            q,
            Solved.get_name(),
        )
        result = self._query_raw(query)

        return self._postprocess_retrieve_packages(result)

    def retrieve_unsolvable_python_packages(self, solver_name: str = None) -> dict:
        """Retrieve a dictionary mapping package names to versions of packages that were marked as unsolvable."""
        if not solver_name:
            filter_str = '@filter(eq(solver_error, true) AND eq(solver_error_unsolvable, true))'
        else:
            filter_str = '@filter(eq(solver_error, true) AND eq(solver_error_unsolvable, true) ' \
                        f'AND eq(solver_name, "{solver_name}"))'

        query = """{
           f(func: has(%s)) @normalize{
               %s %s {
                   package_name:package_name
                   package_version:package_version
                    }
                }
            }""" % (
            Solved.get_name(),
            Solved.get_name(),
            filter_str,
        )
        result = self._query_raw(query)

        return self._postprocess_retrieve_packages(result)

    def retrieve_unsolvable_python_packages_per_run_software_environment(self, solver_name: str) -> dict:
        """Retrieve a dictionary mapping package names to versions of packages that were marked as unsolvable.

        The result is given for a specific run software environment (OS + python version)
        """
        solver_info = self.parse_python_solver_name(solver_name)
        query = """{
           f(func: has(%s)) @filter(eq(os_name, "%s") AND eq(os_version, "%s") """ \
        """AND eq(python_version, "%s") AND eq(solver_error, true) AND eq(solver_error_unsolvable, true) AND has(~%s)) {
                   package_name:package_name
                   package_version:package_version
                }
            }""" % (
            PythonPackageVersion.get_label(),
            solver_info["os_name"],
            solver_info["os_version"],
            solver_info["python_version"],
            Solved.get_name(),
        )
        result = self._query_raw(query)

        return self._postprocess_retrieve_packages(result)

    def retrieve_document_list_of_unsolvable_python_packages(self) -> list:
        """Retrieve a dictionary mapping package names to versions of packages that were marked as unsolvable."""
        query = """{
            f(func: has(%s)) {
                solver_document_id:solver_document_id
                }
            }""" % (
            Solved.get_name(),
        )
        result = self._query_raw(query)
        return [document_id["solver_document_id"] for document_id in result["f"]]

    def retrieve_unparseable_python_packages(self) -> dict:
        """Retrieve a dictionary mapping package names to versions of packages that couldn't be parsed by solver."""
        query = """{
           f(func: has(%s)) @normalize{
               %s @filter(eq(solver_error, true) AND eq(solver_error_unparseable, true)) {
                   package_name:package_name
                   package_version:package_version
                    }
                }
            }""" % (
            Solved.get_name(),
            Solved.get_name(),
        )
        result = self._query_raw(query)

        return self._postprocess_retrieve_packages(result)

    def get_all_python_packages_count(self, without_error: bool = True) -> int:
        """Retrieve number of Python packages stored in the graph database."""
        if not without_error:
            query = (
                """{
                f(func: has(%s)) {
                    package_name
                }
                }"""
                % PythonPackageVersion.get_label()
            )
        else:
            query = (
                """{
                f(func: has(%s)) @filter(eq(solver_error, false)) {
                    package_name
                }
                }"""
                % PythonPackageVersion.get_label()
            )

        result = self._query_raw(query)
        return len(set([python_package["package_name"] for python_package in result["f"]]))

    def get_error_python_packages_count(self, *, unsolvable: bool = False, unparseable: bool = False) -> int:
        """Retrieve number of Python packages stored in the graph database with error flag."""
        if not unsolvable and not unparseable:
            query = (
                """{
                f(func: has(%s)) @filter(eq(solver_error, true) """
                """AND eq(solver_error_unsolvable, false) AND eq(solver_error_unparseable, false)) {
                    c: count(uid)
                }
            }"""
                % PythonPackageVersion.get_label()
            )
        elif unsolvable and not unparseable:
            query = (
                """{
                f(func: has(%s)) @filter(eq(solver_error_unsolvable, true)) {
                    c: count(uid)
                }
                }"""
                % PythonPackageVersion.get_label()
            )
        elif unparseable and not unsolvable:
            query = (
                """{
                f(func: has(%s)) @filter(eq(solver_error_unparseable, true)) {
                    c: count(uid)
                }
                }"""
                % PythonPackageVersion.get_label()
            )
        else:
            raise ValueError("Cannot set both flags - unsolvable and unparseable to retrieve error stats")

        result = self._query_raw(query)
        if len(result["f"]) == 1:
            return result["f"][0]["c"]
        elif len(result["f"]) == 0:
            return 0
        else:
            raise ValueError(f"Internal error - multiple values returned for count query:\n{query}")

    def get_solver_documents_count(self) -> int:
        """Get number of solver documents synced into graph."""
        query = (
            """
        {
            f(func: has(%s)) {
                c: count(uid)
            }
        }
        """
            % EcosystemSolverRun.get_label()
        )
        result = self._query_raw(query)
        return result["f"][0]["c"]

    def get_analyzer_documents_count(self) -> int:
        """Get number of image analysis documents synced into graph."""
        query = (
            """
        {
            f(func: has(%s)) {
                c: count(uid)
            }
        }
        """
            % PackageExtractRun.get_label()
        )
        result = self._query_raw(query)
        return result["f"][0]["c"]

    def retrieve_dependent_packages(self, package_name: str) -> dict:
        """Get mapping package name to package version of packages that depend on the given package."""
        package_name = self.normalize_python_package_name(package_name)
        query = """
        {
            f(func: has(%s)) @filter(eq(package_name, %s)) @normalize @cascade{
                ~%s {
                    package_name:package_name
                    package_version:package_version
                    }
            }
        }
        """ % (
            PythonPackageVersion.get_label(),
            package_name,
            DependsOn.get_name(),
        )
        result = self._query_raw(query)

        return self._postprocess_retrieve_packages(result)

    async def get_python_package_tuple(self, python_package_node_id: int) -> Dict[int, tuple]:
        """Get Python's package name, package version, package index tuple for the given package id."""
        query = (
            """
        {
            q(func: uid(%s)) @cascade {
                package_name
                package_version
                index_url
            }
        }
        """
            % python_package_node_id
        )
        result = self._query_raw(query)["q"]

        if not result:
            raise NotFoundError(f"No package with node id {python_package_node_id} found")

        return {
            python_package_node_id: (result[0]["package_name"], result[0]["package_version"], result[0]["index_url"])
        }

    def _get_python_package_version_by_uid(self, uid: int, *, without_cache: bool = False) -> Optional[dict]:
        """Get Python package version information for the given uid."""
        if not without_cache:
            record = self.cache.get_python_package_version_uid_record(uid)
            if record:
                return record

        query = """
        {
            q(func: uid(%s)) {
                package_name
                package_version
                index_url
                os_name
                os_version
                python_version
            }
        }
        """ % (uid,)
        query_result = self._query_raw(query)["q"]

        if not query_result:
            raise ValueError("No records were found for PythonPackageVersion with uid %r", uid)

        result = dict(
            package_name=query_result["package_name"],
            package_version=query_result["package_version"],
            index_url=query_result["index_url"],
            os_name=query_result.get("os_name"),
            os_version=query_result.get("os_version"),
        )
        if not without_cache:
            self.cache.add_python_package_version_uid_record(**result, uid=uid)

        return result

    def _get_python_package_version_entity_by_uid(
        self,
        uid: int,
        *,
        without_cache: bool = False
    ) -> Optional[Tuple[str, str]]:
        """Get Python package version entity information for the given uid."""
        if not without_cache:
            record = self.cache.get_python_package_version_entity_uid_record(uid)
            if record:
                return record

        query = """
        {
            q(func: uid(%s)) {
                package_name
                package_version
            }
        }
        """ % (uid,)
        query_result = self._query_raw(query)["q"]
        if not query_result:
            raise ValueError("No records were found for PythonPackageVersionEntity with uid %r", uid)

        result = (query_result[0]["package_name"], query_result[0]["package_version"])
        if not without_cache:
            self.cache.add_python_package_version_entity_uid_record(
                package_name=result[0],
                package_version=result[1],
                uid=uid
            )

        return result

    def get_depends_on(
        self,
        package_name: str,
        package_version: str,
        index_url: str,
        *,
        os_name: str = None,
        os_version: str = None,
        python_version: str = None,
        without_cache: bool = False,
    ) -> Set[Tuple[str, str]]:
        """Get dependencies for the given Python package respecting environment.

        If no environment is provided, dependencies are returned for all environments as stored in the database.
        """
        record = locals()
        record.pop("without_cache")
        record.pop("self")

        if not without_cache:
            result = self.cache.get_depends_on(**record)
            if result:
                return result

        query_filter = self._get_query_filter(
            os_name=os_name,
            os_version=os_version,
            python_version=python_version,
        )
        query = """
            {
                q(func: has(%s)) \
                @filter(eq(package_name, "%s") AND eq(package_version, "%s") AND eq(index_url, "%s") %s) {
                    uid
                    depends_on {
                        uid
                    }
            }
        }
        """ % (
            PythonPackageVersion.get_label(),
            package_name,
            package_version,
            index_url,
            "AND " + query_filter if query_filter else '',
        )
        # We always have one element in the query result - the uid itself.
        query_result = self._query_raw(query)["q"]
        query_result = query_result[0]

        result = set()
        for entry in query_result.get("depends_on", []):
            dependency_name, dependency_version = self._get_python_package_version_entity_by_uid(
                entry["uid"],
                without_cache=without_cache,
            )
            result.add((dependency_name, dependency_version))

            if not without_cache:
                self.cache.add_depends_on(
                    **record,
                    dependency_name=dependency_name,
                    dependency_version=dependency_version
                )

        return result

    def get_python_package_version_records(
        self,
        package_name: str,
        package_version: str,
        *,
        os_name: Union[str, None],
        os_version: Union[str, None],
        python_version: Union[str, None],
        without_cache: bool = False,
    ) -> Optional[List[dict]]:
        """Get records for the given package regardless of index_url."""
        if not without_cache:
            result = self.cache.get_python_package_version_records(
                package_name=package_name,
                package_version=package_version,
                os_name=os_name,
                os_version=os_version,
                python_version=python_version,
            )
            if result:
                return result

        query_filter = self._get_query_filter(
            os_name=os_name,
            os_version=os_version,
            python_version=python_version
        )
        query = """
        {
            q(func: has(%s)) @filter(eq(package_name, "%s") \
            AND eq(package_version, "%s") AND has(index_url) AND has(os_name) \
            AND has(os_version) and has(python_version)%s) {
                uid
                package_name
                package_version
                index_url
                os_name
                os_version
                python_version
            }
        }
        """ % (
            PythonPackageVersion.get_label(),
            package_name,
            package_version,
            " AND " + query_filter if query_filter else "",
        )
        query_result = self._query_raw(query)["q"]

        result = []
        for item in query_result:
            entry = dict(
                package_name=item["package_name"],
                package_version=item["package_version"],
                index_url=item["index_url"],
                os_name=item["os_name"],
                os_version=item["os_version"],
                python_version=item["python_version"],
            )
            result.append(entry)

            if not without_cache:
                self.cache.add_python_package_version_uid_record(
                    **entry,
                    uid=item["uid"],
                )

        return result or None

    @staticmethod
    @functools.lru_cache(maxsize=10)
    def _get_query_filter(
        os_name: str = None,
        os_version: str = None,
        python_version: str = None,
    ) -> str:
        """Get a query filter to filter out records based on environment."""
        query_filter = ""
        if os_name:
            query_filter = f'eq(os_name, "{os_name}")'

        if os_version:
            if query_filter:
                query_filter += " AND "
            query_filter += f'eq(os_version, "{os_version}")'

        if python_version:
            if query_filter:
                query_filter += " AND "
            query_filter += f'eq(python_version, "{python_version}")'

        return query_filter

    def retrieve_transitive_dependencies_python(
        self,
        package_name: str,
        package_version: str,
        index_url: str,
        *,
        os_name: str = None,
        os_version: str = None,
        python_version: str = None,
        without_cache: bool = False,
    ) -> Set[Tuple[Tuple[str, str, str], Union[Tuple[str, str, str], None]]]:
        """Get all transitive dependencies for the given package by traversing dependency graph.

        It's much faster to retrieve just dependency ids for the transitive
        dependencies as most of the time is otherwise spent in serialization
        and deserialization of query results. The ids are obtained later on (kept in ids map, see bellow).

        The ids map represents a map to optimize number of retrievals - not to perform duplicate
        queries into graph instance.
        """
        package_name = self.normalize_python_package_name(package_name)
        result = set()
        package_tuple = (package_name, package_version, index_url)
        stack = deque((package_tuple,))
        seen_tuples = {package_tuple}
        while stack:
            package_tuple = stack.pop()

            dependencies = self.get_depends_on(
                package_name=package_tuple[0],
                package_version=package_tuple[1],
                index_url=package_tuple[2],
                os_name=os_name,
                os_version=os_version,
                python_version=python_version,
                without_cache=without_cache,
            )

            for dependency_name, dependency_version in dependencies:
                records = self.get_python_package_version_records(
                    package_name=dependency_name,
                    package_version=dependency_version,
                    os_name=os_name,
                    os_version=os_version,
                    python_version=python_version,
                    without_cache=without_cache,
                )

                if records is None:
                    # Not resolved yet.
                    result.add((
                        package_tuple,
                        (dependency_name, dependency_version, None),
                    ))
                else:
                    for record in records:
                        dependency_tuple = (record["package_name"], record["package_version"], record["index_url"])
                        result.add((package_tuple, dependency_tuple))

                        if dependency_tuple in seen_tuples:
                            continue

                        stack.append(dependency_tuple)
                        seen_tuples.add(dependency_tuple)

        return result

    def retrieve_transitive_dependencies_python_multi(
        self,
        *package_tuples,
        os_name: str = None,
        os_version: str = None,
        python_version: str = None,
    ) -> Dict[Tuple[str, str, str], Set[Tuple[Tuple[str, str, str], Tuple[str, str, str]]]]:
        """Get all transitive dependencies for a given set of packages by traversing the dependency graph."""
        result = {}
        for package_tuple in package_tuples:
            paths = self.retrieve_transitive_dependencies_python(
                package_tuple[0],
                package_tuple[1],
                package_tuple[2],
                os_name=os_name,
                os_version=os_version,
                python_version=python_version,
            )
            result[package_tuple] = paths

        return result

    def solver_records_exist(self, solver_document: dict) -> bool:
        """Check if the given solver document record exists."""
        solver_document_id = SolverResultsStore.get_document_id(solver_document)
        query = """
        {
            f(func: has(%s)) @filter(eq(solver_datetime, "%s") """ \
        """AND eq(solver_document_id, "%s") AND eq(solver_name, %s) AND eq(solver_version, %s)) {
                count(uid)
            }
        }
        """ % (
            Solved.get_name(),
            solver_document["metadata"]["datetime"],
            solver_document_id,
            SolverResultsStore.get_solver_name_from_document_id(solver_document_id),
            solver_document["metadata"]["analyzer_version"],
        )
        result = self._query_raw(query)

        return result["f"][0]["count"] > 0

    def solver_document_id_exist(self, solver_document_id: str) -> bool:
        """Check if there is a solver document record with the given id."""
        query = """
        query q($l: string) {
            f(func: has(%s)) @filter(eq(solver_document_id, "%s")) {
                count(uid)
            }
        }
        """ % (
            EcosystemSolverRun.get_label(),
            solver_document_id,
        )
        result = self._query_raw(query)
        if result["f"][0]["count"] > 1:
            _LOGGER.error(
                f"Integrity error - multiple solver runs found for the same solver document id: {solver_document_id}"
            )

        return result["f"][0]["count"] > 0

    def dependency_monkey_document_id_exist(self, dependency_monkey_document_id: str) -> bool:
        """Check if the given dependency monkey report record exists in the graph database."""
        query = """{
        query q($l: string) {
            f(func: has(%s)) @filter(eq(dependency_monkey_document_id, "%s")) {
                count(uid)
            }
        }
        """ % (
            DependencyMonkeyRun.get_label(),
            dependency_monkey_document_id,
        )
        result = self._query_raw(query)
        if result["f"][0]["count"] > 1:
            _LOGGER.error(
                f"Integrity error - multiple dependency monkey runs found for the "
                f"same dependency monkey document id: {dependency_monkey_document_id}"
            )

        return result["f"][0]["count"] > 0

    def adviser_document_id_exist(self, adviser_document_id: str) -> bool:
        """Check if there is a adviser document record with the given id."""
        query = """{
            f(func: has(%s)) @filter(eq(adviser_document_id, "%s")) {
                count(uid)
            }
        }
        """ % (
            AdviserRun.get_label(),
            adviser_document_id,
        )
        result = self._query_raw(query)
        if result["f"][0]["count"] > 1:
            _LOGGER.error(
                f"Integrity error - multiple adviser runs found for the "
                f"same adviser document id: {adviser_document_id}"
            )

        return result["f"][0]["count"] > 0

    def analysis_records_exist(self, analysis_document: dict) -> bool:
        """Check whether the given analysis document records exist in the graph database."""
        analysis_document_id = AnalysisResultsStore.get_document_id(analysis_document)
        query = (
            """
        {
            f(func: has(%s)) @filter(eq(analysis_datetime, "%s") """
            """AND eq(analysis_document_id, "%s") AND eq(package_extract_name, %s) """
            """AND eq(package_extract_version, %s)) {
                count(uid)
            }
        }
        """
            % (
                PackageExtractRun.get_label(),
                analysis_document["metadata"]["datetime"],
                analysis_document_id,
                analysis_document["metadata"]["analyzer"],
                analysis_document["metadata"]["analyzer_version"],
            )
        )
        result = self._query_raw(query)

        return result["f"][0]["count"] > 0

    def analysis_document_id_exist(self, analysis_document_id: str) -> bool:
        """Check if there is an analysis document record with the given id."""
        query = """{
            f(func: has(%s)) @filter(eq(analysis_document_id, "%s")) {
                count(uid)
            }
        }
        """ % (
            PackageExtractRun.get_label(),
            analysis_document_id,
        )
        result = self._query_raw(query)
        if result["f"][0]["count"] > 1:
            _LOGGER.error(
                f"Integrity error - multiple package-extract runs found for the "
                f"same image analysis document id: {analysis_document_id}"
            )

        return result["f"][0]["count"] > 0

    def package_analysis_document_id_exist(self, package_analysis_document_id: str) -> bool:
        """Check if there is a package analysis document record with the given id."""
        query = """{
            f(func: has(%s)) @filter(eq(package_analysis_document_id, "%s")) {
                count(uid)
            }
        }
        """ % (
            PackageAnalyzerRun.get_label(),
            package_analysis_document_id,
        )
        result = self._query_raw(query)
        if result["f"][0]["count"] > 1:
            _LOGGER.error(
                f"Integrity error - multiple package-analyzer runs found for the "
                f"same package analysis document id: {package_analysis_document_id}"
            )

        return result["f"][0]["count"] > 0

    def inspection_document_id_exist(self, inspection_document_id: str) -> bool:
        """Check if there is an inspection document record with the given id."""
        query = """{
            f(func: has(%s)) @filter(eq(inspection_document_id, "%s")) {
                count(uid)
            }
        }
        """ % (
            InspectionRun.get_label(),
            inspection_document_id,
        )
        result = self._query_raw(query)
        if result["f"][0]["count"] > 1:
            _LOGGER.error(
                f"Integrity error - multiple inspection runs found for the "
                f"same inspection document id: {inspection_document_id}"
            )

        return result["f"][0]["count"] > 0

    def provenance_checker_document_id_exist(self, provenance_checker_document_id: str) -> bool:
        """Check if there is a provenance-checker document record with the given id."""
        query = """{
            f(func: has(%s)) @filter(eq(provenance_checker_document_id, "%s")) {
                count(uid)
            }
        }
        """ % (
            ProvenanceCheckerRun.get_label(),
            provenance_checker_document_id,
        )
        result = self._query_raw(query)
        if result["f"][0]["count"] > 1:
            _LOGGER.error(
                f"Integrity error - multiple provenance checker runs found for the "
                f"same provenance checker document id: {provenance_checker_document_id}"
            )

        return result["f"][0]["count"] > 0

    def get_python_cve_records(self, package_name: str, package_version: str) -> List[dict]:
        """Get known vulnerabilities for the given package-version."""
        package_name = self.normalize_python_package_name(package_name)
        query = """{
            f(func: has(%s)) @filter(eq(ecosystem, "python") """ \
        """AND eq(package_name, "%s") AND eq(package_version, "%s")) {
                v: has_vulnerability {
                    %s
                }
            }
        }
        """ % (
            PythonPackageVersionEntity.get_label(),
            package_name,
            package_version,
            "\n".join(CVE.get_properties().keys()),
        )
        result = self._query_raw(query)
        return list(chain(*(item["v"] for item in result["f"])))

    def get_python_package_version_hashes_sha256(
        self, package_name: str, package_version: str, index_url: str
    ) -> List[str]:
        """Get hashes for a Python package in specified version."""
        package_name = self.normalize_python_package_name(package_name)
        # TODO: we should consider os name, os version and other properties to have this matching for the given env
        query = """{
            f(func: has(%s)) @filter(eq(ecosystem, "python") AND eq(package_name, "%s") """ \
        """AND eq(package_version, "%s") AND eq(index_url, "%s")) {
                a: has_artifact {
                    artifact_hash_sha256
                }
            }
        }
        """ % (
            PythonPackageVersion.get_label(),
            package_name,
            package_version,
            index_url,
        )
        query_result = self._query_raw(query)["f"]

        # Join resulting arrays.
        result = []
        for artifact_record in query_result:
            for item in artifact_record["a"]:
                result.append(item["artifact_hash_sha256"])

        return result

    def get_all_python_package_version_hashes_sha256(self, package_name: str, package_version: str) -> list:
        """Get hashes for a Python package per index."""
        package_name = self.normalize_python_package_name(package_name)

        # The query requires @cascade to filter out the nodes which
        # has index url but not artifact for that package/version
        query = """{
            f(func: has(%s)) @filter(eq(package_name, "%s") AND eq(package_version, "%s")) @cascade{
                index_url
                %s {
                    artifact_hash_sha256
                }
            }
        }
        """ % (
            PythonPackageVersion.get_label(),
            package_name,
            package_version,
            HasArtifact.get_name(),
        )
        result = self._query_raw(query)

        return [[hashes["index_url"], hashes["has_artifact"][0]["artifact_hash_sha256"]] for hashes in result["f"]]

    def register_python_package_index(self, url: str, warehouse_api_url: str = None, verify_ssl: bool = True) -> bool:
        """Register the given Python package index in the graph database."""
        existed = PythonPackageIndex.from_properties(
            url=url, warehouse_api_url=warehouse_api_url, verify_ssl=verify_ssl
        ).get_or_create(self.client)
        return existed

    def python_package_index_listing(self) -> list:
        """Get listing of Python package indexes registered in the graph database database."""
        query = (
            """
        {
            f(func: has(%s)) {
                url
                warehouse_api_url
                verify_ssl
            }
        }
        """
            % PythonPackageIndex.get_label()
        )

        # State explicitly warehouse API url is None if no was configured.
        result = self._query_raw(query)["f"]
        for item in result:
            if "warehouse_api_url" not in item:
                item["warehouse_api_url"] = None

        return result

    def get_python_package_index_urls(self) -> set:
        """Retrieve all the URLs of registered Python package indexes."""
        query = (
            """
        {
            f(func: has(%s)) {
                u: url
            }
        }
        """
            % PythonPackageIndex.get_label()
        )
        result = self._query_raw(query)
        return set(chain(item["u"] for item in result["f"]))

    def get_python_packages_for_index(self, index_url: str) -> Set[str]:
        """Retrieve listing of Python packages known to graph database instance for the given index."""
        query = """
            {
                f(func: has(%s)) @filter(eq(index_url, "%s") AND eq(ecosystem, python)) {
                    package_name
                }
            }
            """ % (
            PythonPackageVersion.get_label(),
            index_url,
        )
        result = self._query_raw(query)
        return set([python_package["package_name"] for python_package in result["f"]])

    def get_python_packages(self) -> Set[str]:
        """Retrieve listing of all Python packages known to graph database instance."""
        query = (
            """
            {
                f(func: has(%s)) @filter(eq(ecosystem, python)) {
                    package_name
                }
            }
            """
            % PythonPackageVersion.get_label()
        )
        result = self._query_raw(query)
        return set([python_package["package_name"] for python_package in result["f"]])

    def _python_packages_create_stack(
        self, python_package_versions: Iterable[PythonPackageVersion], software_stack: SoftwareStackBase
    ) -> None:
        """Assign the given set of packages to the stack."""
        for python_package_version in python_package_versions:
            CreatesStack.from_properties(source=python_package_version, target=software_stack).get_or_create(
                self.client
            )

    def _create_python_package_record(
        self, python_package_version: PythonPackageVersion, verify_index: bool = True
    ) -> None:
        """Create a record for the given Python package.

        @raises PythonIndexNotRegistered: if there is no index registered from which the Python version came.
        """
        assert (
            python_package_version.uid is None
        ), "The given Python package has been already synced into graph database"

        package_index = PythonPackageIndex.query_one(self.client, url=python_package_version.index_url)
        if verify_index and not package_index:
            raise PythonIndexNotRegistered(
                f"Python package index for {python_package_version.index_url} not registered, "
                f"cannot insert package {python_package_version.to_dict()}"
            )

        existed = python_package_version.get_or_create(self.client)

        if not existed:
            entity, _ = self.create_python_package_version_entity(
                python_package_version.package_name,
                python_package_version.package_version,
                python_package_version.index_url
            )

            InstalledFrom.from_properties(source=entity, target=python_package_version).get_or_create(self.client)

            if package_index and not existed:
                ProvidedBy.from_properties(source=entity, target=package_index).get_or_create(self.client)

    def create_python_package_version_entity(
        self,
        package_name: str,
        package_version: str,
        index_url: str,
        *,
        only_if_package_seen: bool = False,
    ) -> Optional[Tuple[PythonPackageVersionEntity, bool]]:
        """Create a Python package version entity in the graph database."""
        if only_if_package_seen and not self.python_package_exists(package_name):
            return None

        entity = PythonPackageVersionEntity.from_properties(
            ecosystem="python",
            package_name=package_name,
            package_version=package_version,
            index_url=index_url,
        )
        existed = entity.get_or_create(self.client)

        return entity, existed

    def create_python_packages_pipfile(
        self, pipfile_locked: dict, run_software_environment: UserRunSoftwareEnvironmentModel = None
    ) -> List[PythonPackageVersion]:
        """Create Python packages from Pipfile.lock entries and return them."""
        result = []
        pipfile_locked = PipfileLock.from_dict(pipfile_locked, pipfile=None)
        for package in pipfile_locked.packages.packages.values():
            python_package_version = PythonPackageVersion.from_properties(
                ecosystem="python",
                package_name=self.normalize_python_package_name(package.name),
                package_version=package.locked_version,
                index_url=package.index.url if package.index else None,
                extras=None,
                os_name=run_software_environment.os_name if run_software_environment else None,
                os_version=run_software_environment.os_version if run_software_environment else None,
                python_version=run_software_environment.python_version if run_software_environment else None,
                # We assume these to be false as these are inputs or recommendation output.
                solver_error=False,
                solver_error_unparseable=False,
                solver_error_unsolvable=False,
            )
            self._create_python_package_record(python_package_version, verify_index=False)
            result.append(python_package_version)

        return result

    def create_user_software_stack_pipfile(
        self,
        adviser_document_id: str,
        pipfile_locked: dict,
        run_software_environment: UserRunSoftwareEnvironmentModel = None,
    ) -> UserSoftwareStack:
        """Create a user software stack entry from Pipfile.lock."""
        python_package_versions = self.create_python_packages_pipfile(pipfile_locked, run_software_environment)
        software_stack = UserSoftwareStack.from_properties(document_id=adviser_document_id)
        software_stack.get_or_create(self.client)
        self._python_packages_create_stack(python_package_versions, software_stack)
        return software_stack

    def create_python_package_requirement(self, requirements: dict) -> List[PythonPackageRequirement]:
        """Create requirements for un-pinned Python packages."""
        result = []
        pipfile = Pipfile.from_dict(requirements)
        for requirement in pipfile.packages.packages.values():
            python_package_requirement = PythonPackageRequirement.from_properties(
                ecosystem="python",
                package_name=self.normalize_python_package_name(requirement.name),
                version_range=requirement.version,
                index_url=requirement.index.url if requirement.index else None,
                markers=requirement.markers,
                develop=requirement.develop,
            )
            python_package_requirement.get_or_create(self.client)
            result.append(python_package_requirement)

        return result

    def create_inspection_software_stack_pipfile(
        self, document_id: str, pipfile_locked: dict
    ) -> InspectionSoftwareStack:
        """Create an inspection software stack entry from Pipfile.lock."""
        python_package_versions = self.create_python_packages_pipfile(pipfile_locked)
        software_stack = InspectionSoftwareStack.from_properties(inspection_document_id=document_id)
        software_stack.get_or_create(self.client)
        self._python_packages_create_stack(python_package_versions, software_stack)
        return software_stack

    def create_advised_software_stack_pipfile(
        self,
        adviser_document_id: str,
        pipfile_locked: dict,
        *,
        advised_stack_index: int,
        performance_score: float,
        overall_score: float,
        run_software_environment: UserRunSoftwareEnvironmentModel,
    ) -> AdvisedSoftwareStack:
        """Create an advised software stack entry from Pipfile.lock."""
        python_package_versions = self.create_python_packages_pipfile(pipfile_locked, run_software_environment)
        software_stack = AdvisedSoftwareStack.from_properties(
            adviser_document_id=adviser_document_id,
            advised_stack_index=advised_stack_index,
            performance_score=performance_score,
            overall_score=overall_score,
        )
        software_stack.get_or_create(self.client)
        self._python_packages_create_stack(python_package_versions, software_stack)
        return software_stack

    @staticmethod
    def _get_hardware_information(specs: dict) -> HardwareInformationModel:
        """Get hardware information based on requests provided."""
        hardware = specs.get("hardware") or {}
        ram_size = OpenShift.parse_memory_spec(specs["memory"]) if specs.get("memory") else None
        if ram_size is not None:
            # Convert bytes to GiB, we need float number for Dgraph serialization
            ram_size = ram_size / (1024 ** 3)

        return HardwareInformationModel.from_properties(
            cpu_family=hardware.get("cpu_family"),
            cpu_model=hardware.get("cpu_model"),
            cpu_physical_cpus=hardware.get("physical_cpus"),
            cpu_model_name=hardware.get("processor"),
            cpu_cores=OpenShift.parse_cpu_spec(specs["cpu"]) if specs.get("cpu") else None,
            ram_size=ram_size,
        )

    @enable_vertex_cache
    def sync_inspection_result(self, document) -> None:
        """Sync the given inspection document into the graph database."""
        # Check if we have such performance model before creating any other records.
        performance_indicator = None
        inspection_document_id = InspectionResultsStore.get_document_id(document)
        if document["specification"].get("script"):  # We have run an inspection job.
            if not isinstance(document["job_log"]["stdout"], dict):
                raise ValueError(
                    "Performance indicator did not produce a valid JSON output in %r: %s",
                    inspection_document_id,
                    document["job_log"]["stdout"],
                )

            if not document["job_log"]["stdout"]:
                raise ValueError("No values provided for inspection output %r", inspection_document_id)

            performance_indicator_name = document["job_log"]["stdout"].get("name")
            performance_model_class = PERFORMANCE_MODEL_BY_NAME.get(performance_indicator_name)
            if not performance_model_class:
                raise PerformanceIndicatorNotRegistered(
                    f"No performance indicator registered for name {performance_indicator_name!r}"
                )

            performance_indicator = performance_model_class.create_from_report(document)
            performance_indicator.get_or_create(self.client)

        build_cpu = OpenShift.parse_cpu_spec(document["specification"]["build"]["requests"]["cpu"])
        build_memory = OpenShift.parse_memory_spec(document["specification"]["build"]["requests"]["memory"])
        run_cpu = OpenShift.parse_cpu_spec(document["specification"]["run"]["requests"]["cpu"])
        run_memory = OpenShift.parse_memory_spec(document["specification"]["run"]["requests"]["memory"])

        # Convert bytes to GiB, we need float number given the fixed int size.
        run_memory = run_memory / (1024 ** 3)
        build_memory = build_memory / (1024 ** 3)

        inspection_run = InspectionRun.from_properties(
            inspection_document_id=inspection_document_id,
            inspection_datetime=document.get("created"),
            amun_version=None,  # TODO: propagate Amun version here which should match API version
            build_requests_cpu=build_cpu,
            build_requests_memory=build_memory,
            run_requests_cpu=run_cpu,
            run_requests_memory=run_memory,
        )
        inspection_run.get_or_create(self.client)

        if "python" in document["specification"]:
            inspection_software_stack = self.create_inspection_software_stack_pipfile(
                inspection_document_id, document["specification"]["python"]["requirements_locked"]
            )
            InspectionStackInput.from_properties(source=inspection_software_stack, target=inspection_run).get_or_create(
                self.client
            )

        # We query for an existing analysis of image for build and run, if it did not exist, we create
        # a placeholder which will be used in package-extract sync.
        build_software_environment = BuildSoftwareEnvironmentModel.query_one(
            self.client, environment_name=inspection_document_id
        )
        if not build_software_environment:
            # TODO: we will need to use fully-qualified images in inspection runs as base.
            build_software_environment = BuildSoftwareEnvironmentModel.from_properties(
                environment_name=document["specification"]["base"]
            )
            build_software_environment.get_or_create(self.client)

        InspectionBuildSoftwareEnvironmentInput.from_properties(
            source=build_software_environment, target=inspection_run
        ).get_or_create(self.client)

        run_software_environment = RunSoftwareEnvironmentModel.query_one(
            self.client, environment_name=inspection_document_id
        )
        if not run_software_environment:
            run_software_environment = RunSoftwareEnvironmentModel.from_properties(
                environment_name=inspection_document_id
            )
            run_software_environment.get_or_create(self.client)

        InspectionRunSoftwareEnvironmentInput.from_properties(
            source=run_software_environment, target=inspection_run
        ).get_or_create(self.client)

        hardware = HardwareInformationConfig.from_dict(
            document["specification"]["build"].get("requests", {}).get("hardware", {})
        )
        hardware_information_build = HardwareInformationModel.from_properties(**hardware.to_dict())
        hardware_information_build.get_or_create(self.client)
        UsedInBuild.from_properties(source=hardware_information_build, target=inspection_run).get_or_create(self.client)

        if document["specification"].get("script"):  # We have run an inspection job.
            hardware = HardwareInformationConfig.from_dict(
                document["specification"]["run"].get("requests", {}).get("hardware", {})
            )
            hardware_information_job = HardwareInformationModel.from_properties(**hardware.to_dict())
            hardware_information_job.get_or_create(self.client)
            UsedInJob.from_properties(source=hardware_information_job, target=inspection_run).get_or_create(self.client)

            ObservedPerformance.from_properties(
                source=inspection_run,
                target=performance_indicator,
                performance_indicator_index=0,  # We can now run only one pi per inspection request.
            ).get_or_create(self.client)

    def create_python_cve_record(
        self,
        package_name: str,
        package_version: str,
        index_url: str,
        *,
        record_id: str,
        version_range: str,
        advisory: str,
        cve: str = None,
    ) -> Tuple[CVE, bool]:
        """Store information about a CVE in the graph database for the given Python package."""
        package_name = self.normalize_python_package_name(package_name)
        python_index = PythonPackageIndex.query_one(self.client, url=index_url)
        if not python_index:
            raise PythonIndexNotRegistered(
                f"Cannot insert CVE record into database, no Python index with url {index_url} registered"
            )

        entity, _ = self.create_python_package_version_entity(package_name, package_version, index_url)
        ProvidedBy.from_properties(source=entity, target=python_index).get_or_create(self.client)

        cve_record = CVE.from_properties(cve_id=record_id, version_range=version_range, advisory=advisory, cve_name=cve)
        cve_record_existed = cve_record.get_or_create(self.client)
        _LOGGER.debug("CVE record wit id %r ", record_id, "added" if not cve_record_existed else "was already present")

        has_vulnerability = HasVulnerability.from_properties(source=entity, target=cve_record)
        has_vulnerability_existed = has_vulnerability.get_or_create(self.client)

        _LOGGER.debug(
            "CVE record %r for vulnerability of %r in version %r ",
            record_id,
            package_name,
            package_version,
            "added" if not has_vulnerability_existed else "was already present",
        )

        return cve_record, has_vulnerability_existed

    def _deb_sync_analysis_result(self, package_extract_run: PackageExtractRun, document: dict) -> None:
        """Sync results of deb packages found in the given container image."""
        for deb_package_info in document["result"]["deb-dependencies"]:
            deb_package_version = DebPackageVersion.from_properties(
                ecosystem="deb",
                package_name=deb_package_info["name"],
                package_version=deb_package_info["version"],
                epoch=deb_package_info.get("epoch"),
                arch=deb_package_info["arch"],
            )
            deb_package_version.get_or_create(self.client)
            Identified.from_properties(source=package_extract_run, target=deb_package_version).get_or_create(
                self.client
            )

            # These three can be grouped with a zip, but that is not that readable...
            for pre_depends in deb_package_info.get("pre-depends") or []:
                deb_dependency = DebDependency.from_properties(ecosystem="deb", package_name=pre_depends["name"])
                deb_dependency.get_or_create(self.client)

                DebPreDepends.from_properties(
                    source=deb_package_version, target=deb_dependency, version_range=pre_depends.get("version")
                ).get_or_create(self.client)

            for depends in deb_package_info.get("depends") or []:
                deb_dependency = DebDependency.from_properties(ecosystem="deb", package_name=depends["name"])
                deb_dependency.get_or_create(self.client)

                DebDepends.from_properties(
                    source=deb_package_version, target=deb_dependency, version_range=depends.get("version")
                ).get_or_create(self.client)

            for replaces in deb_package_info.get("replaces") or []:
                deb_dependency = DebDependency.from_properties(ecosystem="deb", package_name=replaces["name"])
                deb_dependency.get_or_create(self.client)

                DebReplaces.from_properties(
                    source=deb_package_version, target=deb_dependency, version_range=replaces.get("version")
                ).get_or_create(self.client)

    def _rpm_sync_analysis_result(self, package_extract_run: PackageExtractRun, document: dict) -> None:
        """Sync results of RPMs found in the given container image."""
        for rpm_package_info in document["result"]["rpm-dependencies"]:
            rpm_package_version = RPMPackageVersion.from_properties(
                ecosystem="rpm",
                package_name=rpm_package_info["name"],
                package_version=rpm_package_info["version"],
                release=rpm_package_info.get("release"),
                epoch=rpm_package_info.get("epoch"),
                arch=rpm_package_info.get("arch"),
                src=rpm_package_info.get("src", False),
                package_identifier=rpm_package_info.get("package_identifier", rpm_package_info["name"]),
            )
            rpm_package_version.get_or_create(self.client)

            Identified.from_properties(source=package_extract_run, target=rpm_package_version).get_or_create(
                self.client
            )

            for dependency in rpm_package_info["dependencies"]:
                rpm_requirement = RPMRequirement.from_properties(rpm_requirement_name=dependency)
                rpm_requirement.get_or_create(self.client)

                Requires.from_properties(source=rpm_package_version, target=rpm_requirement).get_or_create(self.client)

    def _python_sync_analysis_result(
        self, package_extract_run: PackageExtractRun, document: dict, environment: EnvironmentBase
    ) -> None:
        """Sync results of Python packages found in the given container image."""
        for python_package_info in document["result"]["mercator"] or []:
            if python_package_info["ecosystem"] == "Python-RequirementsTXT":
                # We don't want to sync found requirement.txt artifacts as
                # they do not carry any valuable information for us.
                continue

            if "result" not in python_package_info or "error" in python_package_info["result"]:
                # Mercator was unable to process this - e.g. there was a
                # setup.py that is not distutils setup.py
                _LOGGER.info("Skipping error entry - %r", python_package_info)
                continue

            if not python_package_info["result"].get("name"):
                analysis_document_id = AnalysisResultsStore.get_document_id(document)
                _LOGGER.warning(
                    "No package name found in entry %r when syncing document %r",
                    python_package_info,
                    analysis_document_id,
                )
                continue

            python_package_version = PythonPackageVersion.from_properties(
                ecosystem="python",
                package_name=self.normalize_python_package_name(python_package_info["result"]["name"]),
                package_version=python_package_info["result"]["version"],
                index_url=None,
                extras=None,
                os_name=environment.os_name,
                os_version=environment.os_version,
                python_version=environment.python_version,
                solver_error=False,
                solver_error_unparseable=False,
                solver_error_unsolvable=False,
            )
            self._create_python_package_record(python_package_version, verify_index=False)

            Identified.from_properties(source=package_extract_run, target=python_package_version).get_or_create(
                self.client
            )

    def _python_file_digests_sync_analysis_result(self, package_extract_run: PackageExtractRun, document: dict) -> None:
        """Sync results of Python files found in the given container image."""
        for py_file in document["result"]["python-files"]:
            python_file_digests = PythonFileDigest.from_properties(
                sha256=py_file["sha256"],
            )
            python_file_digests.get_or_create(self.client)

            FoundFile.from_properties(
                source=package_extract_run,
                target=python_file_digests,
                file_path=py_file["filepath"],
            ).get_or_create(self.client)

    @enable_vertex_cache
    def sync_analysis_result(self, document: dict) -> None:
        """Sync the given analysis result to the graph database."""
        analysis_document_id = AnalysisResultsStore.get_document_id(document)
        environment_type = document["metadata"]["arguments"]["thoth-package-extract"]["metadata"]["environment_type"]
        origin = document["metadata"]["arguments"]["thoth-package-extract"]["metadata"]["origin"]
        environment_name = document["metadata"]["arguments"]["extract-image"]["image"]

        image_tag = "latest"
        image_name = environment_name
        parts = environment_name.rsplit(":", maxsplit=1)
        if len(parts) == 2:
            image_name = parts[0]
            image_tag = parts[1]

        # TODO: capture errors on image analysis? result of package-extract should be a JSON with error flag
        package_extract_run = PackageExtractRun.from_properties(
            analysis_document_id=analysis_document_id,
            analysis_datetime=document["metadata"]["datetime"],
            package_extract_version=document["metadata"]["analyzer_version"],
            package_extract_name=document["metadata"]["analyzer"],
            environment_type=environment_type,
            origin=origin,
            debug=document["metadata"]["arguments"]["thoth-package-extract"]["verbose"],
            package_extract_error=False,
            image_tag=image_tag,
            duration=None,  # TODO: assign duration
            os_id=document["result"].get("operating-system", {}).get("id"),
            os_name=document["result"].get("operating-system", {}).get("name"),
            os_version_id=document["result"].get("operating-system", {}).get("version_id"),

        )
        package_extract_run.get_or_create(self.client)

        environment_parameters = {
            "environment_name": environment_name,
            # TODO: find Python version which would be used by default
            "python_version": None,
            "image_name": image_name,
            "image_sha": document["result"]["layers"][-1],
            "os_name": package_extract_run.os_name.lower() if package_extract_run.os_name else None,
            "os_version": package_extract_run.os_version_id,
            # TODO: assign CUDA
        }

        if environment_type == "runtime":
            environment_class = RunSoftwareEnvironmentModel
        elif environment_type == "buildtime":
            environment_class = BuildSoftwareEnvironmentModel
        else:
            raise ValueError("Unknown environment type %r, should be 'buildtime' or 'runtime'" % environment_type)

        environment = environment_class.query_one(self.client, environment_name=environment_name)

        if not environment:
            environment = environment_class.from_properties(**environment_parameters)
            environment.get_or_create(self.client)

        AnalyzedBy.from_properties(source=environment, target=package_extract_run).get_or_create(self.client)

        self._rpm_sync_analysis_result(package_extract_run, document)
        self._deb_sync_analysis_result(package_extract_run, document)
        self._python_sync_analysis_result(package_extract_run, document, environment)
        self._python_file_digests_sync_analysis_result(package_extract_run, document)

    @enable_vertex_cache
    def sync_package_analysis_result(self, document: dict) -> None:
        """Sync the given package analysis result to the graph database."""
        package_analysis_document_id = PackageAnalysisResultsStore.get_document_id(document)
        package_name = document["metadata"]["arguments"]["python"]["package_name"]
        package_version = document["metadata"]["arguments"]["python"]["package_version"]
        index_url = document["metadata"]["arguments"]["python"]["index_url"]

        # TODO: capture errors on package analysis? result of package-analyzer should be a JSON with error flag
        package_analyzer_run = PackageAnalyzerRun.from_properties(
            package_analysis_document_id=package_analysis_document_id,
            package_analysis_datetime=document["metadata"]["datetime"],
            package_analyzer_version=document["metadata"]["analyzer_version"],
            package_analyzer_name=document["metadata"]["analyzer"],
            debug=document["metadata"]["arguments"]["thoth-package-analyzer"]["verbose"],
            package_analyzer_error=False,
            duration=None,  # TODO: assign duration
        )
        package_analyzer_run.get_or_create(self.client)

        python_index = PythonPackageIndex.query_one(self.client, url=index_url)
        if not python_index:
            raise PythonIndexNotRegistered(
                f"Cannot insert PythonPackageVersionEntity record into database, "
                f"no Python index with url {index_url} registered"
            )

        entity, _ = self.create_python_package_version_entity(package_name, package_version, index_url)
        ProvidedBy.from_properties(
            source=entity,
            target=python_index,
        ).get_or_create(self.client)

        PackageAnalyzerInput.from_properties(
            source=entity,
            target=package_analyzer_run,
        ).get_or_create(self.client)

        for artifact in document["result"][index_url]["artifacts"]:
            python_artifact = PythonArtifact.from_properties(
                artifact_hash_sha256=artifact["sha256"],
                artifact_name=artifact["name"],
            )
            python_artifact.get_or_create(self.client)

            Investigated.from_properties(
                source=package_analyzer_run,
                target=python_artifact,
            ).get_or_create(self.client)

            for digest in artifact["digests"]:
                filepath = digest["filepath"]
                if filepath.endswith(".py"):
                    python_file_digests = PythonFileDigest.from_properties(
                        sha256=digest["sha256"],
                    )
                    python_file_digests.get_or_create(self.client)

                    FoundFile.from_properties(
                        source=package_analyzer_run,
                        target=python_file_digests,
                        file_path=filepath,
                    ).get_or_create(self.client)

                    IncludedFile.from_properties(
                        source=python_artifact,
                        target=python_file_digests,
                        package_analysis_document_id=package_analysis_document_id,
                    ).get_or_create(self.client)
                else:
                    _LOGGER.warning("File %r found inside artifact not synced", filepath)

    @enable_vertex_cache
    def sync_solver_result(self, document: dict) -> None:
        """Sync the given solver result to the graph database."""
        solver_document_id = SolverResultsStore.get_document_id(document)
        solver_name = SolverResultsStore.get_solver_name_from_document_id(solver_document_id)
        solver_info = self.parse_python_solver_name(solver_name)
        solver_datetime = document["metadata"]["datetime"]
        solver_version = document["metadata"]["analyzer_version"]
        os_name = solver_info["os_name"]
        os_version = solver_info["os_version"]
        python_version = solver_info["python_version"]

        ecosystem_solver_run = EcosystemSolverRun.from_properties(
            ecosystem="python",
            solver_document_id=solver_document_id,
            solver_datetime=solver_datetime,
            solver_name=solver_name,
            solver_version=solver_version,
            os_name=os_name,
            os_version=os_version,
            python_version=python_version,
            duration=None,  # TODO: propagate duration information
        )
        ecosystem_solver_run.get_or_create(self.client)

        for python_package_info in document["result"]["tree"]:
            package_name = python_package_info["package_name"]
            package_version = python_package_info["package_version"]
            index_url = python_package_info["index_url"]

            python_package_version = PythonPackageVersion.from_properties(
                ecosystem="python",
                package_name=self.normalize_python_package_name(package_name),
                package_version=package_version,
                index_url=index_url,
                os_name=ecosystem_solver_run.os_name,
                os_version=ecosystem_solver_run.os_version,
                python_version=ecosystem_solver_run.python_version,
                solver_error=False,
                solver_error_unparseable=False,
                solver_error_unsolvable=False,
            )
            self._create_python_package_record(python_package_version, verify_index=True)
            Solved.from_properties(source=ecosystem_solver_run, target=python_package_version).get_or_create(
                self.client
            )

            if not python_package_info["sha256"]:
                _LOGGER.error(
                    f"No hashes found for package {package_name} in version {package_version} from {index_url}, "
                    f"error during syncing solver document {solver_document_id}"
                )

            for digest in python_package_info["sha256"]:
                python_artifact = PythonArtifact.from_properties(artifact_hash_sha256=digest)
                python_artifact.get_or_create(self.client)
                HasArtifact.from_properties(source=python_package_version, target=python_artifact).get_or_create(
                    self.client
                )

            # TODO: detect and store extras
            # TODO: detect and store markers
            for dependency in python_package_info["dependencies"]:
                for index_entry in dependency["resolved_versions"]:
                    for dependency_version in index_entry["versions"]:
                        dependency_entity = PythonPackageVersionEntity.from_properties(
                            ecosystem="python",
                            package_name=self.normalize_python_package_name(dependency["package_name"]),
                            package_version=dependency_version,
                        )
                        dependency_entity.get_or_create(self.client)

                        DependsOn.from_properties(
                            source=python_package_version,
                            target=dependency_entity,
                            version_range=dependency.get("required_version") or "*",
                        ).get_or_create(self.client)

        for error_info in document["result"]["errors"]:
            package_name = error_info.get("package_name") or error_info["package"]
            package_version = error_info["version"]
            index_url = error_info["index"]

            python_package_version = PythonPackageVersion.from_properties(
                ecosystem="python",
                package_name=self.normalize_python_package_name(package_name),
                package_version=package_version,
                index_url=index_url,
                os_name=ecosystem_solver_run.os_name,
                os_version=ecosystem_solver_run.os_version,
                python_version=ecosystem_solver_run.python_version,
                solver_error=True,
                solver_error_unparseable=False,
                solver_error_unsolvable=False,
                is_provided=error_info.get("is_provided"),
            )
            self._create_python_package_record(python_package_version, verify_index=True)
            Solved.from_properties(source=ecosystem_solver_run, target=python_package_version).get_or_create(
                self.client
            )

        for unsolvable in document["result"]["unresolved"]:
            if not unsolvable["version_spec"].startswith("=="):
                # No resolution can be perfomed so no identifier is captured, report warning and continue.
                # We would like to capture this especially when there are
                # packages in ecosystem that we cannot find (e.g. not configured private index
                # or removed package).
                _LOGGER.warning(
                    "Cannot sync unsolvable package %r as package is not locked to as specific version", unsolvable
                )
                continue

            package_name = unsolvable["package_name"]
            index_url = unsolvable["index"]
            package_version = unsolvable["version_spec"][len("=="):]

            python_package_version = PythonPackageVersion.from_properties(
                package_name=self.normalize_python_package_name(package_name),
                package_version=package_version,
                index_url=index_url,
                os_name=ecosystem_solver_run.os_name,
                os_version=ecosystem_solver_run.os_version,
                python_version=ecosystem_solver_run.python_version,
                solver_error=True,
                solver_error_unparseable=False,
                solver_error_unsolvable=True,
            )
            self._create_python_package_record(python_package_version, verify_index=True)
            Solved.from_properties(source=ecosystem_solver_run, target=python_package_version).get_or_create(
                self.client
            )

        for unparsed in document["result"]["unparsed"]:
            parts = unparsed["requirement"].rsplit("==", maxsplit=1)
            if len(parts) != 2:
                # This request did not come from graph-refresh job as there is not pinned version.
                _LOGGER.warning(
                    "Cannot sync unparsed package %r as package is not locked to as specific version", unparsed
                )
                continue

            package_name, package_version = parts
            python_package_version = PythonPackageVersion.from_properties(
                package_name=self.normalize_python_package_name(package_name),
                package_version=package_version,
                index_url=None,
                os_name=ecosystem_solver_run.os_name,
                os_version=ecosystem_solver_run.os_version,
                python_version=ecosystem_solver_run.python_version,
                solver_error=True,
                solver_error_unparseable=True,
                solver_error_unsolvable=False,
            )
            self._create_python_package_record(python_package_version, verify_index=True)
            Solved.from_properties(source=ecosystem_solver_run, target=python_package_version).get_or_create(
                self.client
            )

    @staticmethod
    def _runtime_environment_conf2models(
        runtime_properties: dict, is_user_run: bool = False
    ) -> Tuple[HardwareInformationModel, RunSoftwareEnvironmentModel]:
        """Convert runtime environment configuration into model representatives."""
        hardware_properties = runtime_properties.pop("hardware", {})

        if is_user_run:
            hardware_information = UserHardwareInformationModel.from_properties(**hardware_properties)

            runtime_environment_config = RuntimeEnvironmentConfig.from_dict(runtime_properties)
            # We construct our own name as we do not trust user's name input (it can be basically anything).
            run_software_environment_name = (
                f"{runtime_environment_config.operating_system.name or 'unknown'}"
                f":{runtime_environment_config.operating_system.version or 'unknown'}"
            )

            # TODO: assign image_name and image_sha once we will have this info present in Thoth's configuration file
            run_software_environment = UserRunSoftwareEnvironmentModel.from_properties(
                environment_name=run_software_environment_name,
                python_version=runtime_environment_config.python_version,
                os_name=runtime_environment_config.operating_system.name,
                os_version=runtime_environment_config.operating_system.version,
                cuda_version=runtime_environment_config.cuda_version,
            )

        else:
            hardware_information = HardwareInformationModel.from_properties(**hardware_properties)

            runtime_environment_config = RuntimeEnvironmentConfig.from_dict(runtime_properties)
            # We construct our own name as we do not trust user's name input (it can be basically anything).
            run_software_environment_name = (
                f"{runtime_environment_config.operating_system.name or 'unknown'}"
                f":{runtime_environment_config.operating_system.version or 'unknown'}"
            )

            # TODO: assign image_name and image_sha once we will have this info present in Thoth's configuration file
            run_software_environment = RunSoftwareEnvironmentModel.from_properties(
                environment_name=run_software_environment_name,
                python_version=runtime_environment_config.python_version,
                os_name=runtime_environment_config.operating_system.name,
                os_version=runtime_environment_config.operating_system.version,
                cuda_version=runtime_environment_config.cuda_version,
            )

        return hardware_information, run_software_environment

    @enable_vertex_cache
    def sync_adviser_result(self, document: dict) -> None:
        """Sync adviser result into graph database."""
        adviser_document_id = AdvisersResultsStore.get_document_id(document)
        cli_arguments = document["metadata"]["arguments"]["thoth-adviser"]
        origin = (cli_arguments.get("metadata") or {}).get("origin")

        if not origin:
            _LOGGER.warning("No origin stated in the adviser result %r", adviser_document_id)

        parameters = document["result"]["parameters"]
        adviser_run = AdviserRun.from_properties(
            adviser_document_id=adviser_document_id,
            adviser_datetime=document["metadata"]["datetime"],
            adviser_version=document["metadata"]["analyzer_version"],
            adviser_name=document["metadata"]["analyzer"],
            count=parameters["count"],
            limit=parameters["limit"],
            origin=origin,
            debug=cli_arguments.get("verbose", False),
            limit_latest_versions=parameters.get("limit_latest_versions"),
            adviser_error=document["result"]["error"],
            recommendation_type=parameters["recommendation_type"],
            requirements_format=parameters["requirements_format"],
            duration=None,  # TODO: assign duration
            advised_configuration_changes=bool(document["result"].get("advised_configuration")),
            additional_stack_info=bool(document["result"].get("stack_info")),
        )
        adviser_run.get_or_create(self.client)

        # Runtime Environment Information (Hardware + Software information).
        hardware_information, run_software_environment = self._runtime_environment_conf2models(
            document["result"]["parameters"]["runtime_environment"], is_user_run=True
        )
        hardware_information.get_or_create(self.client)
        run_software_environment.get_or_create(self.client)

        UsedIn.from_properties(source=hardware_information, target=adviser_run).get_or_create(self.client)

        AdviserRunSoftwareEnvironmentInput.from_properties(
            source=run_software_environment, target=adviser_run
        ).get_or_create(self.client)

        # Input stack.
        if document["result"]["input"]["requirements_locked"]:
            # User provided a Pipfile.lock, we can sync it.
            user_software_stack = self.create_user_software_stack_pipfile(
                adviser_document_id, document["result"]["input"]["requirements_locked"], run_software_environment
            )
            AdviserStackInput.from_properties(source=user_software_stack, target=adviser_run).get_or_create(self.client)

        python_package_requirements = self.create_python_package_requirement(
            document["result"]["input"]["requirements"]
        )
        for python_package_requirement in python_package_requirements:
            RequirementsInput.from_properties(source=python_package_requirement, target=adviser_run).get_or_create(
                self.client
            )

        # Output stack.
        for idx, result in enumerate(document["result"]["report"]):
            if len(result) != 3:
                _LOGGER.warning("Omitting stack as no output Pipfile.lock was provided")
                continue

            # result[0] is score report
            # result[1]["requirements"] is Pipfile
            # result[1]["requirements_locked"] is Pipfile.lock
            performance_score = None
            overall_score = None
            for entry in result[0] or []:
                if "performance_score" in entry:
                    if performance_score is not None:
                        _LOGGER.error(
                            "Multiple performance score entries found in %r (index: %d)", adviser_document_id, idx
                        )
                    performance_score = entry["performance_score"]

                if "overall_score" in entry:
                    if overall_score is not None:
                        _LOGGER.error(
                            "Multiple overall score entries found in %r (index: %d)", adviser_document_id, idx
                        )
                    overall_score = entry["overall_score"]

            if result[1] and result[1].get("requirements_locked"):
                advised_software_stack = self.create_advised_software_stack_pipfile(
                    adviser_document_id,
                    (result[1] or {}).get("requirements_locked") or [],
                    advised_stack_index=idx,
                    performance_score=performance_score,
                    overall_score=overall_score,
                    run_software_environment=run_software_environment,
                )
                Advised.from_properties(source=adviser_run, target=advised_software_stack).get_or_create(
                    self.client
                )

    @enable_vertex_cache
    def sync_provenance_checker_result(self, document: dict) -> None:
        """Sync provenance checker results into graph database."""
        provenance_checker_document_id = ProvenanceResultsStore.get_document_id(document)
        origin = (document["metadata"]["arguments"]["thoth-adviser"].get("metadata") or {}).get("origin")

        if not origin:
            _LOGGER.warning("No origin stated in the provenance-checker result %r", provenance_checker_document_id)

        provenance_checker_run = ProvenanceCheckerRun.from_properties(
            provenance_checker_document_id=provenance_checker_document_id,
            provenance_checker_datetime=document["metadata"]["datetime"],
            provenance_checker_version=document["metadata"]["analyzer_version"],
            provenance_checker_name=document["metadata"]["analyzer"],
            origin=origin,
            debug=document["metadata"]["arguments"]["thoth-adviser"]["verbose"],
            provenance_checker_error=document["result"]["error"],
            duration=None,  # TODO: assign duration
        )
        provenance_checker_run.get_or_create(self.client)

        user_input = document["result"]["input"]
        if user_input.get("requirements_locked"):
            # We do not have any runtime information.
            user_software_stack = self.create_user_software_stack_pipfile(
                provenance_checker_document_id, user_input["requirements_locked"]
            )
            ProvenanceCheckerStackInput.from_properties(
                source=user_software_stack, target=provenance_checker_run
            ).get_or_create(self.client)

        if user_input.get("requirements"):
            python_package_requirements = self.create_python_package_requirement(user_input["requirements"])
            for python_package_requirement in python_package_requirements:
                RequirementsInput.from_properties(
                    source=python_package_requirement, target=provenance_checker_run
                ).get_or_create(self.client)

    @enable_vertex_cache
    def sync_dependency_monkey_result(self, document: dict) -> None:
        """Sync reports of dependency monkey runs."""
        # TODO: implement
        dependency_monkey_run = DependencyMonkeyRun.from_properties(
            dependency_monkey_document_id=DependencyMonkeyReportsStore.get_document_id(document),
            dependency_monkey_datetime=document["metadata"]["datetime"],
            dependency_monkey_name=document["metadata"]["analyzer"],
            dependency_monkey_version=document["metadata"]["analyzer_version"],
            seed=document["result"]["parameters"].get("seed"),
            decision=document["result"]["parameters"].get("decision"),
            count=document["result"]["parameters"].get("count"),
            limit_latest_versions=document["result"]["parameters"].get("limit_latest_versions"),
            debug=document["metadata"]["arguments"]["thoth-adviser"]["verbose"],
            dependency_monkey_error=document["result"]["error"],
            duration=None,  # TODO: assign duration
        )
        dependency_monkey_run.get_or_create(self.client)

        python_package_requirements = self.create_python_package_requirement(
            document["result"]["parameters"]["requirements"]
        )
        for python_package_requirement in python_package_requirements:
            RequirementsInput.from_properties(
                source=python_package_requirement, target=dependency_monkey_run
            ).get_or_create(self.client)

        hardware_information, run_software_environment = self._runtime_environment_conf2models(
            document["result"]["parameters"]["runtime_environment"]
        )

        hardware_information.get_or_create(self.client)
        run_software_environment.get_or_create(self.client)

        DependencyMonkeyRunSoftwareEnvironmentInput.from_properties(
            source=run_software_environment, target=dependency_monkey_run
        ).get_or_create(self.client)

        UsedIn.from_properties(source=hardware_information, target=dependency_monkey_run).get_or_create(self.client)

        for inspection_document_id in document["result"]["output"]:
            inspection_software_stack = InspectionSoftwareStack.from_properties(
                inspection_document_id=inspection_document_id
            )
            inspection_software_stack.get_or_create(self.client)

            Resolved.from_properties(source=dependency_monkey_run, target=inspection_software_stack).get_or_create(
                self.client
            )

    def get_number_of_each_vertex_in_graph(self) -> dict:
        """Retrieve dictionary with number of vertices per vertex label in the graph database."""
        node_labels = [model.get_label() for model in ALL_MODELS if issubclass(model, VertexBase)]
        tot_nodes_per_label = {}
        for node_label in node_labels:
            query = (
                """
            {
                f(func: has(%s)) {
                    c:count(uid)
                }
            }
            """
                % node_label
            )
            result = self._query_raw(query)
            count = result["f"][0]["c"]
            tot_nodes_per_label[node_label] = count
        return tot_nodes_per_label

    def get_all_pi_per_framework_count(self, framework: str) -> dict:
        """Retrieve dictionary with number of Performance Indicators per ML Framework in the graph database."""
        pi_labels = [
            model.get_label() for model in ALL_PERFORMANCE_MODELS if issubclass(model, PerformanceIndicatorBase)
        ]
        tot_pi_per_type = {}
        for pi_label in pi_labels:
            query = """
            {
                f(func: has(%s)) @filter(eq(framework, "%s")){
                    c: count(uid)
                }
            }
            """ % (
                pi_label,
                framework,
            )
            result = self._query_raw(query)
            count = result["f"][0]["c"]
            tot_pi_per_type[pi_label] = count
        return tot_pi_per_type
