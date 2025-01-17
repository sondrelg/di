from __future__ import annotations

import inspect
from typing import Any, Mapping, Optional

from di import Container, Dependant, Depends


class Request:
    def __init__(self, headers: Mapping[str, str]) -> None:
        self.headers = {k.lower(): v for k, v in headers.items()}


class HeaderDependant(Dependant[Any]):
    def __init__(self, alias: Optional[str]) -> None:
        self.alias = alias
        super().__init__(call=None, scope=None, share=False)

    def register_parameter(self, param: inspect.Parameter) -> HeaderDependant:
        if self.alias is not None:
            name = self.alias
        else:
            name = param.name.replace("_", "-")

        def get_header(request: Request = Depends()) -> str:
            return param.annotation(request.headers[name])

        self.call = get_header
        # We could return a copy here to allow the same Dependant
        # to be used in multiple places like
        # dep = HeaderDependant(...)
        # def func1(abcd = dep): ...
        # def func2(efgh = dep): ...
        # In this scenario, `dep` would be modified in func2 to set
        # the header name to "efgh", which leads to incorrect results in func1
        # The solution is to return a copy here instead of self, so that
        # the original instance is never modified in place
        return self


def Header(alias: Optional[str] = None) -> Any:
    return HeaderDependant(alias=alias)  # type: ignore


async def web_framework() -> None:
    container = Container()

    valid_request = Request(headers={"x-header-one": "one", "x-header-two": "2"})
    with container.bind(Dependant(lambda: valid_request), Request):
        await container.execute_async(container.solve(Dependant(controller)))  # success

    invalid_request = Request(headers={"x-header-one": "one"})
    with container.bind(Dependant(lambda: invalid_request), Request):
        try:
            await container.execute_async(
                container.solve(Dependant(controller))
            )  # fails
        except KeyError:
            pass
        else:
            raise AssertionError(
                "This call should have failed because x-header-two is missing"
            )


def controller(
    x_header_one: str = Header(), header_two_val: int = Header(alias="x-header-two")
) -> None:
    """This is the only piece of user code"""
    assert x_header_one == "one"
    assert header_two_val == 2
