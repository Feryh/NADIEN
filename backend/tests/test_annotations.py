"""Annotations must resolve: Python 3.14 evaluates them lazily, so a missing import only breaks on 3.12 (CI, Docker)."""

import importlib
import inspect
import pkgutil
import typing

import pytest

import app

MODULES = [info.name for info in pkgutil.walk_packages(app.__path__, "app.")
           if ".migrations." not in info.name and not info.name.endswith("__main__")]


@pytest.mark.parametrize("module_name", MODULES)
def test_should_resolve_function_annotations_when_module_is_imported(module_name):
    module = importlib.import_module(module_name)
    for name, func in inspect.getmembers(module, inspect.isfunction):
        if func.__module__ == module_name:
            typing.get_type_hints(func)
