from __future__ import annotations

import functools
import typing
from collections import deque
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from typing import (
    Any,
    Awaitable,
    Deque,
    Dict,
    Iterable,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

from di._utils.inspect import is_async_gen_callable, is_gen_callable
from di.api.dependencies import DependantBase
from di.api.executor import AsyncTaskInfo, SyncTaskInfo, TaskInfo
from di.api.providers import DependencyProvider
from di.api.scopes import Scope
from di.exceptions import IncompatibleDependencyError


class ExecutionState(typing.NamedTuple):
    stacks: Mapping[Scope, Union[AsyncExitStack, ExitStack]]
    results: Dict[Task, Any]
    dependency_counts: MutableMapping[Task, int]
    dependants: Mapping[Task, Iterable[Task]]


DependencyType = TypeVar("DependencyType")

TaskQueue = Deque[Optional[TaskInfo]]


class Task:
    __slots__ = ("dependant", "call", "positional_parameters", "keyword_parameters")
    call: DependencyProvider

    def __init__(
        self,
        dependant: DependantBase[Any],
        positional_parameters: Iterable[Task],
        keyword_parameters: Mapping[str, Task],
    ) -> None:
        self.dependant = dependant
        assert dependant.call is not None
        self.call = dependant.call
        self.positional_parameters = positional_parameters
        self.keyword_parameters = keyword_parameters

    def compute(
        self,
        state: ExecutionState,
    ) -> Union[Awaitable[Iterable[Optional[TaskInfo]]], Iterable[Optional[TaskInfo]]]:
        """Compute this dependency within the context of the current state"""
        raise NotImplementedError

    def as_executor_task(self, state: ExecutionState) -> TaskInfo:
        """Bind the state and return either an AsyncTaskInfo or SyncTaskInfo"""
        raise NotImplementedError

    def gather_params(
        self, results: Dict[Task, Any]
    ) -> Tuple[Iterable[Any], Mapping[str, Any]]:
        """Gather all parameters (aka *args and **kwargs) needed for computation"""
        return (
            (results[t] for t in self.positional_parameters),
            {k: results[t] for k, t in self.keyword_parameters.items()},
        )

    def gather_new_tasks(self, state: ExecutionState) -> Iterable[Optional[TaskInfo]]:
        """Look amongst our dependants to see if any of them are now dependency free"""
        new_tasks: TaskQueue = deque()
        for dependant in state.dependants[self]:
            state.dependency_counts[dependant] -= 1
            if state.dependency_counts[dependant] == 0:
                # this dependant has no further dependencies, so we can compute it now
                new_tasks.append(dependant.as_executor_task(state))
        # remove ourselves if we have no deps
        if state.dependency_counts[self] == 0:
            del state.dependency_counts[self]
            if not state.dependency_counts:
                # all dependencies are taken care of, insert sentinel None value
                new_tasks.append(None)
        return new_tasks

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({repr(self.dependant)})"


class AsyncTask(Task):
    __slots__ = ("is_generator",)

    def __init__(
        self,
        dependant: DependantBase[Any],
        positional_parameters: Iterable[Task],
        keyword_parameters: Mapping[str, Task],
    ) -> None:
        super().__init__(dependant, positional_parameters, keyword_parameters)
        self.is_generator = is_async_gen_callable(self.call)
        if self.is_generator:
            self.call = asynccontextmanager(self.call)  # type: ignore[arg-type]

    def as_executor_task(self, state: ExecutionState) -> AsyncTaskInfo:
        return AsyncTaskInfo(
            dependant=self.dependant,
            task=functools.partial(self.compute, state),
        )

    async def compute(
        self,
        state: ExecutionState,
    ) -> Iterable[Optional[TaskInfo]]:
        args, kwargs = self.gather_params(state.results)

        if self.is_generator:
            stack = state.stacks[self.dependant.scope]
            try:
                enter = stack.enter_async_context  # type: ignore[union-attr]
            except AttributeError:
                raise IncompatibleDependencyError(
                    f"The dependency {self.dependant} is an awaitable dependency"
                    f" and canot be used in the sync scope {self.dependant.scope}"
                )
            state.results[self] = await enter(
                self.call(*args, **kwargs)  # type: ignore[arg-type]
            )
        else:
            state.results[self] = await self.call(*args, **kwargs)  # type: ignore[misc]

        return self.gather_new_tasks(state)


class SyncTask(Task):
    __slots__ = ("is_generator",)

    def __init__(
        self,
        dependant: DependantBase[Any],
        positional_parameters: Iterable[Task],
        keyword_parameters: Mapping[str, Task],
    ) -> None:
        super().__init__(dependant, positional_parameters, keyword_parameters)
        self.is_generator = is_gen_callable(self.call)
        if self.is_generator:
            self.call = contextmanager(self.call)  # type: ignore[arg-type]

    def as_executor_task(self, state: ExecutionState) -> SyncTaskInfo:
        return SyncTaskInfo(
            dependant=self.dependant,
            task=functools.partial(self.compute, state),
        )

    def compute(
        self,
        state: ExecutionState,
    ) -> Iterable[Optional[TaskInfo]]:

        args, kwargs = self.gather_params(state.results)

        if self.is_generator:
            stack = state.stacks[self.dependant.scope]
            state.results[self] = stack.enter_context(
                self.call(*args, **kwargs)  # type: ignore[arg-type]
            )
        else:
            state.results[self] = self.call(*args, **kwargs)

        return self.gather_new_tasks(state)