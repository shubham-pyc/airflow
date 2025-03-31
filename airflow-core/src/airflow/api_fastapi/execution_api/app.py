# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from functools import cached_property
from typing import TYPE_CHECKING, Any

import attrs
import svcs
from cadwyn import (
    Cadwyn,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from airflow.api_fastapi.auth.tokens import JWTValidator, get_sig_validation_args

if TYPE_CHECKING:
    import httpx

import structlog

logger = structlog.get_logger(logger_name=__name__)

__all__ = [
    "create_task_execution_api_app",
    "lifespan",
]


def _jwt_validator():
    from airflow.configuration import conf

    required_claims = frozenset(["aud", "exp", "iat"])

    if issuer := conf.get("api_auth", "jwt_issuer", fallback=None):
        required_claims = required_claims | {"iss"}
    validator = JWTValidator(
        required_claims=required_claims,
        issuer=issuer,
        audience=conf.get_mandatory_list_value("execution_api", "jwt_audience"),
        **get_sig_validation_args(make_secret_key_if_needed=False),
    )
    return validator


@svcs.fastapi.lifespan
async def lifespan(app: FastAPI, registry: svcs.Registry):
    app.state.lifespan_called = True

    # According to svcs's docs this shouldn't be needed, but something about SubApps is odd, and we need to
    # record this here
    app.state.svcs_registry = registry

    # Create an app scoped validator, so that we don't have to fetch it every time
    registry.register_value(JWTValidator, _jwt_validator(), ping=JWTValidator.status)
    yield


class CadwynWithOpenAPICustomization(Cadwyn):
    # Workaround lack of customzation https://github.com/zmievsa/cadwyn/issues/255
    async def openapi_jsons(self, req: Request) -> JSONResponse:
        resp = await super().openapi_jsons(req)
        open_apischema = json.loads(resp.body)  # type: ignore[arg-type]
        open_apischema = self.customize_openapi(open_apischema)

        resp.body = resp.render(open_apischema)

        return resp

    def customize_openapi(self, openapi_schema: dict[str, Any]) -> dict[str, Any]:
        """
        Customize the OpenAPI schema to include additional schemas not tied to specific endpoints.

        This is particularly useful for client SDKs that require models for types
        not directly exposed in any endpoint's request or response schema.

        References:
            - https://fastapi.tiangolo.com/how-to/extending-openapi/#modify-the-openapi-schema
        """
        extra_schemas = get_extra_schemas()
        for schema_name, schema in extra_schemas.items():
            if schema_name not in openapi_schema["components"]["schemas"]:
                openapi_schema["components"]["schemas"][schema_name] = schema

        # The `JsonValue` component is missing any info. causes issues when generating models
        openapi_schema["components"]["schemas"]["JsonValue"] = {
            "title": "Any valid JSON value",
            "anyOf": [
                {"type": t} for t in ("string", "number", "integer", "object", "array", "boolean", "null")
            ],
        }

        for comp in openapi_schema["components"]["schemas"].values():
            for prop in comp.get("properties", {}).values():
                # {"type": "string", "const": "deferred"}
                # to
                # {"type": "string", "enum": ["deferred"]}
                #
                # this produces better results in the code generator
                if prop.get("type") == "string" and (const := prop.pop("const", None)):
                    prop["enum"] = [const]

        return openapi_schema


def create_task_execution_api_app() -> FastAPI:
    """Create FastAPI app for task execution API."""
    from airflow.api_fastapi.execution_api.routes import execution_api_router
    from airflow.api_fastapi.execution_api.versions import bundle

    # See https://docs.cadwyn.dev/concepts/version_changes/ for info about API versions
    app = CadwynWithOpenAPICustomization(
        title="Airflow Task Execution API",
        description="The private Airflow Task Execution API.",
        lifespan=lifespan,
        api_version_parameter_name="Airflow-API-Version",
        api_version_default_value=bundle.versions[0].value,
        versions=bundle,
    )

    app.generate_and_include_versioned_routers(execution_api_router)

    # As we are mounted as a sub app, we don't get any logs for unhandled exceptions without this!
    @app.exception_handler(Exception)
    def handle_exceptions(request: Request, exc: Exception):
        logger.exception("Handle died with an error", exc_info=(type(exc), exc, exc.__traceback__))
        content = {"message": "Internal server error"}
        if "correlation-id" in request.headers:
            content["correlation-id"] = request.headers["correlation-id"]
        return JSONResponse(status_code=500, content=content)

    return app


def get_extra_schemas() -> dict[str, dict]:
    """Get all the extra schemas that are not part of the main FastAPI app."""
    from airflow.api_fastapi.execution_api.datamodels.taskinstance import TaskInstance
    from airflow.executors.workloads import BundleInfo
    from airflow.utils.state import TerminalTIState

    return {
        "TaskInstance": TaskInstance.model_json_schema(),
        "BundleInfo": BundleInfo.model_json_schema(),
        # Include the combined state enum too. In the datamodels we separate out SUCCESS from the other states
        # as that has different payload requirements
        "TerminalTIState": {"type": "string", "enum": list(TerminalTIState)},
    }


@attrs.define()
class InProcessExecutionAPI:
    """
    A helper class to make it possible to run the ExecutionAPI "in-process".

    The sync version of this makes use of a2wsgi which runs the async loop in a separate thread. This is
    needed so that we can use the sync httpx client
    """

    _app: FastAPI | None = None
    _cm: AsyncExitStack | None = None

    @cached_property
    def app(self):
        if not self._app:
            from airflow.api_fastapi.execution_api.app import create_task_execution_api_app
            from airflow.api_fastapi.execution_api.deps import JWTBearerDep
            from airflow.api_fastapi.execution_api.routes.connections import has_connection_access
            from airflow.api_fastapi.execution_api.routes.variables import has_variable_access
            from airflow.api_fastapi.execution_api.routes.xcoms import has_xcom_access

            self._app = create_task_execution_api_app()

            async def always_allow(): ...

            self._app.dependency_overrides[JWTBearerDep.dependency] = always_allow
            self._app.dependency_overrides[has_connection_access] = always_allow
            self._app.dependency_overrides[has_variable_access] = always_allow
            self._app.dependency_overrides[has_xcom_access] = always_allow

        return self._app

    @cached_property
    def transport(self) -> httpx.WSGITransport:
        import asyncio

        import httpx
        from a2wsgi import ASGIMiddleware

        middleware = ASGIMiddleware(self.app)

        # https://github.com/abersheeran/a2wsgi/discussions/64
        async def start_lifespan(cm: AsyncExitStack, app: FastAPI):
            await cm.enter_async_context(app.router.lifespan_context(app))

        self._cm = AsyncExitStack()

        asyncio.run_coroutine_threadsafe(start_lifespan(self._cm, self.app), middleware.loop)
        return httpx.WSGITransport(app=middleware)  # type: ignore[arg-type]

    @cached_property
    def atransport(self) -> httpx.ASGITransport:
        import httpx

        return httpx.ASGITransport(app=self.app)
