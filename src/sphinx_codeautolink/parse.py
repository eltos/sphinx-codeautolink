"""Analyse AST of code blocks to determine used names and their sources."""
import ast
import sys
import builtins

from contextlib import contextmanager
from enum import Enum
from functools import wraps
from importlib import import_module
from typing import Dict, Union, List, Optional, Tuple
from dataclasses import dataclass, field

from .warn import logger, warn_type


def parse_names(source: str, doctree_node) -> List['Name']:
    """Parse names from source."""
    tree = ast.parse(source)
    visitor = ImportTrackerVisitor(doctree_node)
    visitor.visit(tree)
    return sum([split_access(a) for a in visitor.accessed], [])


def linenos(node: ast.AST) -> Tuple[int, int]:
    """Return lineno and end_lineno safely."""
    return node.lineno, getattr(node, 'end_lineno', node.lineno)


@dataclass
class Component:
    """Name access component."""

    name: str
    lineno: int
    end_lineno: int
    context: str  # as in ast.Load / Store / Del

    @classmethod
    def from_ast(cls, node):
        """Generate a Component from an AST node."""
        context = 'load'
        if isinstance(node, ast.Name):
            name = node.id
            context = node.ctx.__class__.__name__.lower()
        elif isinstance(node, ast.Attribute):
            name = node.attr
            context = node.ctx.__class__.__name__.lower()
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.Call):
            name = NameBreak.call
        else:
            raise ValueError(f'Invalid AST for component: {node.__class__.__name__}')
        return cls(name, *linenos(node), context)


@dataclass
class PendingAccess:
    """Pending name access."""

    components: List[Component]


@dataclass
class PendingAssign:
    """
    Pending assign target.

    `targets` represent the assignment targets.
    If a single PendingAccess is found, it should be used to store the value
    on the right hand side of the assignment. If multiple values are found,
    they should overwrite any names in the current scope and not assign values.
    """

    targets: Union[Optional[PendingAccess], List[Optional[PendingAccess]]]


class NameBreak(str, Enum):
    """Elements that break name access chains."""

    call = '()'


class LinkContext(str, Enum):
    """Context in which a link appears."""

    none = 'none'
    after_call = 'after_call'
    import_from = 'import_from'  # from *mod.sub* import foo
    import_target = 'import_target'  # from mod.sub import *foo*


@dataclass
class Name:
    """A name accessed in the source traced back to an import."""

    import_components: List[str]
    code_str: str
    lineno: int
    end_lineno: int
    context: LinkContext = None
    resolved_location: str = None


@dataclass
class Access:
    """
    Accessed import, to be broken down into suitable chunks.

    :attr:`prior_components` are components that are implicitly used via
    the base name in :attr:`components`, which is the part that shows on the line.
    :attr:`hidden_components` is an attribute of split Access, in which the
    proper components are not moved to prior components to track which were
    present on the line of the access.

    The base component that connects an import to the name that was used to
    access it is automatically removed from the components in :attr:`full_components`.
    """

    context: LinkContext
    prior_components: List[Component]
    components: List[Component]
    hidden_components: List[Component] = field(default_factory=list)

    @property
    def full_components(self):
        """All components from import base to used components."""
        if not self.prior_components:
            # Import statement itself
            return self.hidden_components + self.components

        if self.hidden_components:
            proper_components = self.hidden_components[1:] + self.components
        else:
            proper_components = self.components[1:]
        return self.prior_components + proper_components

    @property
    def code_str(self):
        """Code representation of components."""
        breaks = set(NameBreak)
        return '.'.join(c.name for c in self.components if c.name not in breaks)

    @property
    def lineno_span(self) -> Tuple[int, int]:
        """Estimate the lineno span of components."""
        min_ = min(c.lineno for c in self.components)
        max_ = max(c.end_lineno for c in self.components)
        return min_, max_


def split_access(access: Access) -> List[Name]:
    """Split access into multiple names."""
    split = [access]
    while True:
        current = split[-1]
        for i, comp in enumerate(current.components):
            if i and comp.name == NameBreak.call:
                hidden = current.hidden_components + current.components[:i]
                next_ = Access(
                    LinkContext.after_call,
                    current.prior_components,
                    current.components[i:],
                    hidden_components=hidden,
                )
                current.components = current.components[:i]
                split.append(next_)
                break
        else:
            break
    if split[-1].components[-1].name == NameBreak.call:
        split.pop()
    return [
        Name(
            [c.name for c in s.full_components],
            s.code_str,
            *s.lineno_span,
            context=s.context,
        )
        for s in split
    ]


@dataclass
class Assignment:
    """Assignment of value to name."""

    to: List[PendingAssign]
    value: Optional[PendingAccess]


def track_parents(func):
    """
    Track a stack of nodes to determine the position of the current node.

    Uses and increments the surrounding classes :attr:`_parents`.
    """
    @wraps(func)
    def wrapper(self: 'ImportTrackerVisitor', *args, **kwargs):
        self._parents += 1
        r: Union[PendingAccess, Assignment, None] = func(self, *args, **kwargs)
        self._parents -= 1
        if not self._parents:
            if isinstance(r, Assignment):
                self._resolve_assignment(r)
            elif isinstance(r, PendingAccess):
                self._handle_access(r)
        return r
    return wrapper


builtin_components: Dict[str, List[Component]] = {
    b: [Component(b, -1, -1, LinkContext.none)] for b in dir(builtins)
}


class ImportTrackerVisitor(ast.NodeVisitor):
    """Track imports and their use through source code."""

    def __init__(self, doctree_node):
        super().__init__()
        self.accessed: List[Access] = []
        self.in_augassign = False
        self._parents = 0
        self.doctree_node = doctree_node

        # Stack for dealing with class body pseudo scopes
        # which are completely bypassed by inner scopes (func, lambda).
        # Current values are copied to the next class body level.
        self.pseudo_scopes_stack: List[Dict[str, List[Component]]] = [
            builtin_components.copy()
        ]
        # Stack for dealing with nested scopes.
        # Holds references to the values of previous nesting levels.
        self.outer_scopes_stack: List[Dict[str, List[Component]]] = []

    @contextmanager
    def reset_parents(self):
        """Reset parents state for the duration of the context."""
        self._parents, old = (0, self._parents)
        yield
        self._parents = old

    track_nodes = (
        ast.Name,
        ast.Attribute,
        ast.Call,
        ast.Assign,
        ast.AnnAssign,
        ast.arg,
    )
    if sys.version_info >= (3, 8):
        track_nodes += (ast.NamedExpr,)

    def visit(self, node: ast.AST):
        """Override default visit to track name access and assignments."""
        if not isinstance(node, self.track_nodes):
            with self.reset_parents():
                return super().visit(node)

        return super().visit(node)

    def _overwrite(self, name: str):
        """Overwrite name in current scope."""
        # Technically dotted values could now be bricked,
        # but we can't prevent the earlier values in the chain from being used.
        # There is a chance that the value which was assigned is a something
        # that we could follow, but for now it's not really worth the effort.
        # With a dotted value, the following condition will never hold as long
        # as the dotted components of imports are discarded on creating the import.
        self.pseudo_scopes_stack[-1].pop(name, None)

    def _assign(self, local_name: str, components: List[Component]):
        """Import or assign a name."""
        self._overwrite(local_name)  # Technically unnecessary unless we follow dots
        self.pseudo_scopes_stack[-1][local_name] = components

    def _create_access(
        self, scope_key: str, new_components: List[Component]
    ) -> Optional[Access]:
        prior = self.pseudo_scopes_stack[-1].get(scope_key, None)
        if prior is None:
            return

        access = Access(LinkContext.none, prior, new_components)
        self.accessed.append(access)
        return access

    def _handle_access(self, access: PendingAccess) -> Optional[Access]:
        components = access.components

        context = components[0].context
        if context == 'store' and not self.in_augassign:
            self._overwrite(components[0].name)
            return

        access = self._create_access(components[0].name, components)
        if context == 'del':
            self._overwrite(components[0].name)
        return access

    def _resolve_assignment(self, assignment: Assignment):
        value = assignment.value
        access = self._handle_access(value) if value is not None else None

        for assign in assignment.to:
            if assign is None or assign.targets is None:
                continue
            elif isinstance(assign.targets, PendingAccess):
                # Single target, we're good to proceed normally
                targets = [assign.targets]
            else:
                # Multiple nested targets, only overwrite assigned names
                access = None
                targets = assign.targets

            for target in targets:
                if target is None:
                    continue

                if len(target.components) == 1:
                    comp = target.components[0]
                    self._overwrite(comp.name)
                    if access is not None:
                        self._assign(comp.name, access.full_components)
                        self._create_access(comp.name, target.components)
                else:
                    self._handle_access(target)

    def _access_simple(self, name: str, lineno: int) -> Optional[Access]:
        component = Component(name, lineno, lineno, 'load')
        return self._create_access(component.name, [component])

    def visit_Global(self, node: ast.Global):
        """Import from top scope."""
        if not self.outer_scopes_stack:
            return  # in outermost scope already, no-op for imports

        imports = self.outer_scopes_stack[0]
        for name in node.names:
            self._overwrite(name)
            if name in imports:
                self._assign(name, imports[name])
                self._access_simple(name, node.lineno)

    def visit_Nonlocal(self, node: ast.Nonlocal):
        """Import from intermediate scopes."""
        imports_stack = self.outer_scopes_stack[1:]
        for name in node.names:
            self._overwrite(name)
            for imports in imports_stack[::-1]:
                if name in imports:
                    self.pseudo_scopes_stack[-1][name] = imports[name]
                    self._access_simple(name, node.lineno)
                    break

    def visit_Import(self, node: Union[ast.Import, ast.ImportFrom], prefix: str = ''):
        """Register import source."""
        import_star = (node.names[0].name == '*')
        if import_star:
            try:
                mod = import_module(node.module)
                import_names = [
                    name for name in mod.__dict__ if not name.startswith('_')
                ]
                aliases = [None] * len(import_names)
            except ImportError:
                logger.warning(
                    f'Could not import module `{node.module}` for parsing!',
                    type=warn_type,
                    subtype='import_star',
                    location=self.doctree_node,
                )
                import_names = []
                aliases = []
        else:
            import_names = [name.name for name in node.names]
            aliases = [name.asname for name in node.names]

        prefix_parts = prefix.rstrip('.').split('.') if prefix else []
        prefix_components = [
            Component(n, *linenos(node), 'load') for n in prefix_parts
        ]
        if prefix:
            self.accessed.append(Access(LinkContext.import_from, [], prefix_components))

        for import_name, alias in zip(import_names, aliases):
            if not import_star:
                components = [
                    Component(n, *linenos(node), 'load')
                    for n in import_name.split('.')
                ]
                self.accessed.append(
                    Access(LinkContext.import_target, [], components, prefix_components)
                )

            if not alias and '.' in import_name:
                # equivalent to only import top level module since we don't
                # follow assignments and the outer modules also get imported
                import_name = import_name.split('.')[0]

            full_components = [
                Component(n, *linenos(node), 'store')
                for n in (prefix + import_name).split('.')
            ]
            self._assign(alias or import_name, full_components)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        """Register import source."""
        if node.level:  # relative import
            for name in node.names:
                self._overwrite(name.asname or name.name)
        else:
            self.visit_Import(node, prefix=node.module + '.')

    @track_parents
    def visit_Name(self, node: ast.Name):
        """Visit a Name node."""
        return PendingAccess([Component.from_ast(node)])

    @track_parents
    def visit_Attribute(self, node: ast.Attribute):
        """Visit an Attribute node."""
        inner: Optional[PendingAccess] = self.visit(node.value)
        if inner is not None:
            inner.components.append(Component.from_ast(node))
        return inner

    @track_parents
    def visit_Call(self, node: ast.Call):
        """Visit a Call node."""
        inner: Optional[PendingAccess] = self.visit(node.func)
        if inner is not None:
            inner.components.append(Component.from_ast(node))
        with self.reset_parents():
            for arg in node.args + node.keywords:
                self.visit(arg)
            if hasattr(node, 'starargs'):
                self.visit(node.starargs)
            if hasattr(node, 'kwargs'):
                self.visit(node.kwargs)
        return inner

    @track_parents
    def visit_Tuple(self, node: ast.Tuple):
        """Visit a Tuple node."""
        if isinstance(node.ctx, ast.Store):
            accesses = []
            for element in node.elts:
                ret = self.visit(element)
                if isinstance(ret, PendingAccess) or ret is None:
                    accesses.append(ret)
                else:
                    accesses.extend(ret)
            return accesses
        else:
            with self.reset_parents():
                for element in node.elts:
                    self.visit(element)

    @track_parents
    def visit_Assign(self, node: ast.Assign):
        """Visit an Assign node."""
        value = self.visit(node.value)
        targets = [PendingAssign(self.visit(n)) for n in node.targets[::-1]]
        return Assignment(targets, value)

    @track_parents
    def visit_AnnAssign(self, node: ast.AnnAssign):
        """Visit an AnnAssign node."""
        value = self.visit(node.value) if node.value is not None else None
        annot = self.visit(node.annotation)
        if annot is not None:
            if value is not None:
                self._handle_access(value)

            annot.components.append(
                Component(NameBreak.call, *linenos(node.annotation), 'load')
            )
            value = annot

        target = self.visit(node.target)
        if value is not None:
            return Assignment([PendingAssign(target)], value)

    def visit_AugAssign(self, node: ast.AugAssign):
        """Visit an AugAssign node."""
        self.visit(node.value)
        self.in_augassign, temp = (True, self.in_augassign)
        self.visit(node.target)
        self.in_augassign = temp

    @track_parents
    def visit_NamedExpr(self, node):
        """Visit a NamedExpr node."""
        value = self.visit(node.value)
        target = self.visit(node.target)
        return Assignment([PendingAssign(target)], value)

    def visit_AsyncFor(self, node: ast.AsyncFor):
        """Delegate to sync for."""
        self.visit_For(node)

    def visit_For(self, node: Union[ast.For, ast.AsyncFor]):
        """Swap node order."""
        self.visit(node.iter)
        self.visit(node.target)
        for n in node.body:
            self.visit(n)
        for n in node.orelse:
            self.visit(n)

    def visit_ClassDef(self, node: ast.ClassDef):
        """Handle pseudo scope of class body."""
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw)

        self._overwrite(node.name)
        self.pseudo_scopes_stack.append(self.pseudo_scopes_stack[0].copy())
        for b in node.body:
            self.visit(b)
        self.pseudo_scopes_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Delegate to func def."""
        self.visit_FunctionDef(node)

    @staticmethod
    def _get_args(node: ast.arguments):
        posonly = getattr(node, 'posonlyargs', [])  # only on 3.8+
        return node.args + node.kwonlyargs + posonly

    def visit_FunctionDef(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]):
        """Swap node order and separate inner scope."""
        self._overwrite(node.name)
        for dec in node.decorator_list:
            self.visit(dec)
        for d in node.args.defaults + node.args.kw_defaults:
            if d is None:
                continue
            self.visit(d)
        args = self._get_args(node.args)
        args += [node.args.vararg, node.args.kwarg]

        inner = self.__class__(self.doctree_node)
        inner.pseudo_scopes_stack[0] = self.pseudo_scopes_stack[0].copy()
        inner.outer_scopes_stack = list(self.outer_scopes_stack)
        inner.outer_scopes_stack.append(self.pseudo_scopes_stack[0])

        for arg in args:
            if arg is None:
                continue
            inner.visit(arg)
        if node.returns is not None:
            self.visit(node.returns)
        for n in node.body:
            inner.visit(n)
        self.accessed.extend(inner.accessed)

    @track_parents
    def visit_arg(self, arg: ast.arg):
        """Handle function argument and its annotation."""
        target = PendingAccess([Component.from_ast(arg)])
        if arg.annotation is not None:
            value = self.visit(arg.annotation)
            if value is not None:
                value.components.append(
                    Component(NameBreak.call, *linenos(arg), 'load')
                )
        else:
            value = None
        return Assignment([PendingAssign(target)], value)

    def visit_Lambda(self, node: ast.Lambda):
        """Swap node order and separate inner scope."""
        for d in node.args.defaults + node.args.kw_defaults:
            if d is None:
                continue
            self.visit(d)
        args = self._get_args(node.args)
        args += [node.args.vararg, node.args.kwarg]

        inner = self.__class__(self.doctree_node)
        inner.pseudo_scopes_stack[0] = self.pseudo_scopes_stack[0].copy()
        for arg in args:
            if arg is None:
                continue
            inner._overwrite(arg.arg)
        inner.visit(node.body)
        self.accessed.extend(inner.accessed)

    def visit_ListComp(self, node: ast.ListComp):
        """Delegate to generic comp."""
        self.visit_generic_comp([node.elt], node.generators)

    def visit_SetComp(self, node: ast.SetComp):
        """Delegate to generic comp."""
        self.visit_generic_comp([node.elt], node.generators)

    def visit_DictComp(self, node: ast.DictComp):
        """Delegate to generic comp."""
        self.visit_generic_comp([node.key, node.value], node.generators)

    def visit_GeneratorExp(self, node: ast.GeneratorExp):
        """Delegate to generic comp."""
        self.visit_generic_comp([node.elt], node.generators)

    def visit_comprehension(self, node: ast.comprehension):
        """Swap node order."""
        self.visit(node.iter)
        self.visit(node.target)
        for f in node.ifs:
            self.visit(f)

    def visit_generic_comp(
        self, values: List[ast.AST], generators: List[ast.comprehension]
    ):
        """Separate inner scope, respects class body scope."""
        inner = self.__class__(self.doctree_node)
        inner.pseudo_scopes_stack[0] = self.pseudo_scopes_stack[-1].copy()
        for gen in generators:
            inner.visit(gen)
        for value in values:
            inner.visit(value)
        self.accessed.extend(inner.accessed)
