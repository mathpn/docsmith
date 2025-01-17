import argparse
import ast
from functools import partial
from typing import Literal, Protocol

import libcst as cst
from ollama import ChatResponse, chat
from pydantic import BaseModel

PROMPT_FILL = """
You are a coding assistant whose task is to generate docstrings for existing code.
You will receive code without any docstrings.
Generate the appropiate docstrings for each function, class or method.

Do not return any code. Use the context only to learn about the code.
Write documentation only for the code provided as input code.

The docstring prescribes the function or method’s effect as a command, not as a description; e.g. don’t write “Returns the pathname …”.
Do not explain implementation details, do not include information about arguments and return here.
If the docstring is multiline, the first line should be a very short summary, followed by a blank line and a more ellaborate description.
Write single-line docstrings if the function is simple.
The docstring for a class should summarize its behavior and list the public methods (one by line) and instance variables.

In the Argument object, describe each argument. In the return object, describe the returned values of the function, if any.

You will receive JSON template below. Fill the slots marked with <SLOT> with the appropriate description. Return as JSON.

{CONTEXT}

Input code:

```python
{CODE}
```

Output template:

```json
{TEMPLATE}
```
"""


INDENT = "    "


class Argument(BaseModel):
    name: str
    description: str
    annotation: str | None = None
    default: str | None = None


class Return(BaseModel):
    description: str
    annotation: str | None


class Docstring(BaseModel):
    node_type: Literal["class", "function"]
    name: str
    docstring: str
    args: list[Argument] | None = None
    ret: Return | None = None


class Documentation(BaseModel):
    entries: list[Docstring]


class DocstringGenerator(Protocol):
    def __call__(
        self, input_code: str, context: str, template: Documentation
    ) -> Documentation: ...


def create_docstring_node(docstring_text: str, indent: str) -> cst.BaseStatement:
    lines = docstring_text.strip().split("\n")

    indented_lines = []
    for line in lines:
        indented_lines.append(indent + line if line.strip() else line)

    return cst.SimpleStatementLine(
        body=[
            cst.Expr(
                value=cst.SimpleString(
                    value=f'"""\n{"\n".join(indented_lines)}\n{indent}"""'
                )
            )
        ]
    )


class DocstringTransformer(cst.CSTTransformer):
    def __init__(self, docstring_generator: DocstringGenerator, module: cst.Module):
        self._current_class: str | None = None
        self._doc: Documentation | None = None
        self.module: cst.Module = module
        self.docstring_gen = docstring_generator
        self.indentation_level = 0

    def visit_Module(self, node):
        self.module = node
        return True

    def visit_FunctionDef(self, node):
        self.indentation_level += 1

    def visit_ClassDef(self, node) -> bool | None:
        self.indentation_level += 1
        self._current_class = node.name.value
        source_lines = cst.Module([node]).code
        # TODO add context
        template = extract_signatures(self.module, node)
        context = get_context(self.module, node)
        doc = self.docstring_gen(source_lines, context, template)
        self._doc = doc
        return super().visit_ClassDef(node)

    def _modify_docstring(self, body, new_docstring):
        # If body is an IndentedBlock, extract its body
        if isinstance(body, cst.IndentedBlock):
            body_statements = list(body.body)
        elif not isinstance(body, list):
            return body
        else:
            body_statements = list(body)

        # Check if first statement is a docstring
        if (
            body_statements
            and isinstance(body_statements[0], cst.SimpleStatementLine)
            and isinstance(body_statements[0].body[0], cst.Expr)
            and isinstance(body_statements[0].body[0].value, cst.SimpleString)
        ):
            # Replace existing docstring
            new_docstring_node = cst.SimpleStatementLine(
                body=[cst.Expr(value=cst.SimpleString(f'"""{new_docstring}"""'))]
            )
            body_statements[0] = new_docstring_node

        # No existing docstring - add new one if provided
        elif new_docstring:
            indent = INDENT * (self.indentation_level + 1)
            new_docstring_node = create_docstring_node(new_docstring, indent)
            body_statements.insert(0, new_docstring_node)

        # Reconstruct the body
        if isinstance(body, cst.IndentedBlock):
            return body.with_changes(body=tuple(body_statements))
        return tuple(body_statements)

    def leave_FunctionDef(self, original_node, updated_node):
        self.indentation_level -= 1
        source_lines = cst.Module([updated_node]).code

        name = updated_node.name.value
        # TODO add context
        if self._current_class is None:
            template = extract_signatures(self.module, updated_node)
            context = get_context(self.module, updated_node)
            doc = self.docstring_gen(source_lines, context, template)
        elif self._doc is not None:
            doc = self._doc
        else:
            return updated_node

        new_docstring = find_docstring_by_name(doc, name)
        if new_docstring is None:
            print("oh no")
            print(updated_node.name)
            print(doc)
            return updated_node

        new_body = self._modify_docstring(
            updated_node.body, docstring_to_str(new_docstring)
        )

        return updated_node.with_changes(body=new_body)

    def leave_ClassDef(self, original_node, updated_node):
        self.indentation_level -= 1
        self._current_class = None

        if self._doc is None:
            return updated_node

        new_docstring = find_docstring_by_name(self._doc, updated_node.name.value)

        if new_docstring is None:
            print("oh no 2")
            print(updated_node.name)
            print(self._doc)
            return updated_node

        new_body = self._modify_docstring(
            updated_node.body, docstring_to_str(new_docstring)
        )

        return updated_node.with_changes(body=new_body)


def find_function_definitions(tree) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    function_defs = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_defs.append(node)

    return function_defs


def find_class_definitions(tree) -> list[ast.ClassDef]:
    function_defs = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            function_defs.append(node)

    return function_defs


def find_top_level_definitions(
    tree,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
    return definitions


def collect_entities(
    node,
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef],
) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    entities = set()

    for node in ast.walk(node):
        match node:
            case ast.Call(func=ast.Name(name)):
                entities.add(definitions.get(name))
            case (
                ast.AnnAssign(annotation=ast.Name(name))
                | ast.arg(annotation=ast.Name(name))
            ):
                entities.add(definitions.get(name))
            case (
                ast.AnnAssign(
                    annotation=ast.Subscript(
                        value=ast.Name(subs_name), slice=ast.Name(name)
                    )
                )
                | ast.arg(
                    annotation=ast.Subscript(
                        value=ast.Name(subs_name), slice=ast.Name(name)
                    )
                )
            ):
                entities.add(definitions.get(name))
                entities.add(definitions.get(subs_name))

    return list(e for e in entities if e is not None)


def get_context(module: cst.Module, node: cst.CSTNode) -> str:
    source = module.code

    tree = ast.parse(source)
    definitions = find_top_level_definitions(tree)

    node_source = module.code_for_node(node)
    node_tree = ast.parse(node_source)
    referenced_functions = collect_entities(node_tree, definitions)

    out = "\n".join(ast.unparse(func) for func in referenced_functions)
    return out


def has_return_stmt(node):
    return any(
        isinstance(n, ast.Return) and n.value is not None for n in ast.walk(node)
    )


def extract_signatures(module: cst.Module, node: cst.CSTNode) -> Documentation:
    source = module.code_for_node(node)

    tree = ast.parse(source)
    function_defs = find_function_definitions(tree)
    # TODO argument
    function_defs = filter(lambda x: not is_private(x), function_defs)
    function_defs = filter(lambda x: not is_dunder(x), function_defs)

    class_defs = find_class_definitions(tree)
    class_defs = filter(lambda x: not is_private(x), class_defs)

    function_entries = [extract_signature(node) for node in function_defs]
    class_entries = [
        Docstring(node_type="class", name=node.name, docstring="<SLOT>")
        for node in class_defs
    ]

    return Documentation(entries=[*class_entries, *function_entries])


def is_private(node):
    name = node.name
    return name.startswith("_") and not is_dunder(node)


def is_dunder(node):
    name = node.name
    return name.startswith("__") and name.endswith("__")


def extract_signature(function_node: ast.FunctionDef | ast.AsyncFunctionDef):
    function_name = function_node.name

    arguments = []
    for arg in function_node.args.args:
        arg_name = arg.arg

        if arg_name in {"self", "cls"}:
            continue

        arg_type = ast.unparse(arg.annotation) if arg.annotation else None

        default_value = None
        if function_node.args.defaults:
            num_defaults = len(function_node.args.defaults)

            # Align defaults with arguments
            # TODO double check
            default_index = len(function_node.args.args) - num_defaults
            if function_node.args.args.index(arg) >= default_index:
                default_value = ast.unparse(
                    function_node.args.defaults[
                        function_node.args.args.index(arg) - default_index
                    ]
                )

        arguments.append(
            Argument(
                name=arg_name,
                annotation=arg_type,
                default=default_value,
                description="<SLOT>",
            )
        )

    # Handle *args
    if function_node.args.vararg:
        arguments.append(
            Argument(
                name=f"*{function_node.args.vararg.arg}",
                annotation=ast.unparse(function_node.args.vararg.annotation)
                if function_node.args.vararg.annotation
                else None,
                description="<SLOT>",
            )
        )

    # Handle **kwargs
    if function_node.args.kwarg:
        arguments.append(
            Argument(
                name=f"**{function_node.args.kwarg.arg}",
                annotation=ast.unparse(function_node.args.kwarg.annotation)
                if function_node.args.kwarg.annotation
                else None,
                description="<SLOT>",
            )
        )

    # Extract return type
    ret = None
    if has_return_stmt(function_node):
        return_type = (
            ast.unparse(function_node.returns) if function_node.returns else None
        )
        ret = Return(description="<SLOT>", annotation=return_type)

    return Docstring(
        node_type="function",
        name=function_name,
        docstring="<SLOT>",
        args=arguments,
        ret=ret,
    )


def find_docstring_by_name(doc: Documentation, name: str) -> Docstring | None:
    entries = [entry for entry in doc.entries if entry.name == name]
    return entries[0] if entries else None


def docstring_to_str(docstring: Docstring) -> str:
    args_strings = []
    for arg in docstring.args or []:
        if arg.annotation is not None:
            arg_string = f"    {arg.name} ({arg.annotation}): {arg.description}"
        else:
            arg_string = f"    {arg.name}: {arg.description}"
        if arg.default is not None:
            arg_string += f" (default {arg.default})"
        args_strings.append(arg_string)

    string = f"{docstring.docstring}\n"

    if args_strings:
        string += f"""\nParameters:
-----------

{"\n".join(args_strings)}
"""

    # XXX
    if docstring.ret is not None and (
        docstring.ret.description or docstring.ret.annotation
    ):
        if docstring.ret.annotation:
            ret_string = f"{docstring.ret.annotation} : {docstring.ret.description}"
        else:
            ret_string = f"{docstring.ret.description}"

        string += f"""\nReturns:
--------

    {ret_string}
"""
    return string


def ollama_docstring_generator(
    input_code: str, context: str, template: Documentation, model: str = "qwen2.5-coder"
) -> Documentation:
    context = f"Important context:\n\n```python\n{context}\n```" if context else ""
    response: ChatResponse = chat(
        model=model,
        messages=[
            {
                "role": "user",
                "content": PROMPT_FILL.format(
                    CONTEXT=context,
                    CODE=input_code,
                    TEMPLATE=template.model_dump_json(),
                ),
            }
        ],
        format=Documentation.model_json_schema(),
    )
    return Documentation.model_validate_json(response.message.content)


def read_source(fpath: str):
    with open(fpath, "r") as f:
        source = f.read()
    return source


def modify_docstring(source_code, docstring_generator: DocstringGenerator):
    module = cst.parse_module(source_code)
    modified_module = module.visit(DocstringTransformer(docstring_generator, module))
    return modified_module.code


def main():
    parser = argparse.ArgumentParser(
        description="Modify Python source code to add docstrings."
    )
    parser.add_argument(
        "filepath", type=str, help="Path to the Python source file to be modified."
    )
    parser.add_argument("--model", type=str, help="LLM model to be used")
    args = parser.parse_args()

    source = read_source(args.filepath)

    docstring_generator = ollama_docstring_generator
    if args.model:
        docstring_generator = partial(docstring_generator, model=args.model)

    modified_source = modify_docstring(source, ollama_docstring_generator)

    with open(args.filepath, "w") as f:
        f.write(modified_source)


if __name__ == "__main__":
    main()
