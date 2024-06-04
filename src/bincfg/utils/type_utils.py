import bincfg
from .atomic_token_dict import AtomicTokenDict
from typing import Union, Protocol, Iterable, Optional, Any, Literal, NoReturn, Tuple, Mapping, Sequence, TextIO, Callable

# Extra imports for type hints that don't necessarily have to be installed
try:
    import torch
except ImportError:
    pass
try:
    import networkx
except ImportError:
    pass

PlainAddress = Union[int, str]
"""Types that can be converted into an address by themselves, without having to look at any attributes"""

class Addressable(Protocol):
    """Object that has a `.address` attribute which can be converted into a memory address"""
    address: PlainAddress

AddressLike = Union[PlainAddress, Addressable]
"""Objects that can be converted into a memory address, or that have a `.address` attribute which can"""


class NormalizerType(Protocol):
    """Object that has a valid `.normalize()` function"""
    def normalize(self, *strings: str, cfg: 'Optional[bincfg.CFG]', 
                  block: 'Optional[bincfg.CFGBasicBlock]', newline_tup: 'Union[None, Tuple[str, str], object]', 
                  match_instruction_address: 'bool', **kwargs: Any) -> 'list[str]': ...


TokenDictType = Union[dict[str, int], AtomicTokenDict]