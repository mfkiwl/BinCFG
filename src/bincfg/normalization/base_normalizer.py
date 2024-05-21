"""
Classes for normalizing assembly instructions.
"""

import re
import hashlib
import os
import copy
from types import MethodType
from .norm_utils import imm_to_int, DISINFO_START, DISINFO_END, RE_IMMEDIATE, RE_STRING_LITERAL, parse_disinfo_json, \
    scan_for_token
from ..utils import eq_obj, parameter_saver, paramspec_set_class_funcs, hash_obj
from .base_tokenizer import TokenMismatchError, Tokens, INSTRUCTION_START_TOKEN, TokenizationLevel, \
    parse_tokenization_level, UnknownTokenError
from .base_tokenizer import _USE_DEFAULT_NT


# Default threshold for immediate values for normalization methods such as 'safe'
DEFAULT_IMMEDIATE_THRESHOLD = 5000


# Regexs for capturing immediates and string literals at the start of disassembler info tokens
RE_DISINFO_IMM = re.compile(r'({imm}).*'.format(imm=RE_IMMEDIATE))
RE_DISINFO_STR = re.compile(r'({str}).*'.format(str=RE_STRING_LITERAL))


class MetaNorm(type):
    """A metaclass for BaseNormalizer. 

    The Problem:
        If you change instance functions within the __init__ method (EG: see the SAFE _handle_immediate() function
        being changed in __init__), then 'self' will not automatically be passed to those functions.

        NOTE: this is specifically useful when the effect of a normalization method depends on parameters sent to
        the instance, not inherent to the class

        NOTE: this is not the case for any functions that are set during class initialization (EG: outside of the
        __init__() block)

        So, any functions changed within __init__ methods must be altered to also pass 'self'. I ~could~ force the
        users to have to call a '__post_init__()' function or something, but can we count on them (IE: myself) to
        always do that?...

    The Solution:
        This metaclass inserts extra code before and after any normalizer's __init__ method is called. That code keeps
        track of all instance functions before intitialization, and checks to see if any of them change after
        initialization. This means someone re-set a function within __init__ (IE: self._handle_immediate = ...).
        When this happens, 'self' will not automatically be passed when that function is called. These functions
        are then wrapped to also automatically pass 'self'.

        NOTE: to determine if a function changes, we just check equality between previous and new functions using
        getattr(self, func_name). I don't know why basic '==' works but 'is' and checking id's do not, but I'm not 
        going to question it...

        NOTE: We also have to keep track of the instance functions as an instance variable in case a parent class needs
        their function updated, or if a child class also changes a parent class's function in init

    NOTE: this will mean you cannot call all of that class's methods and expect them to always be the same as calling
    instance methods if you change functions in __init__
    """
    def __new__(cls, name, bases, dct):
        ret_cls = super().__new__(cls, name, bases, dct)  # Create a new class object (not instance)

        old_init = ret_cls.__init__  # Save this class's __init__ function to call later
        def insert_post(self, *args, **kwargs):
            """Create the new __init__ function, inserting code before and after the old __init__"""
            # Keep track of all of this instance's functions. Need to do this as an instance variable in case a parent
            #   class changed things in init so it's not wrapped twice in the child class. Also keep track of which
            #   stack frame needs to remove the __instance_funcs__ attribute
            remove_instance_funcs = False
            if not hasattr(self, '__instance_funcs__') or self.__instance_funcs__ is None:
                self.__instance_funcs__ = {k: getattr(self, k) for k in dir(self) if not k.startswith("__") and callable(getattr(self, k))}
                remove_instance_funcs = True

            # Call the old __init__ function
            parameter_saver(old_init, insert_functions=False)(self, *args, **kwargs)

            # Check if any of the functions before are no longer equal. If so, assume we need to change these functions
            #   to pass self. I don't know why basic '==' works but 'is' and checking id's do not, but I'm not going to
            #   question it...
            new_instance_funcs = {k: getattr(self, k) for k in dir(self) if k in self.__instance_funcs__ and self.__instance_funcs__[k] != getattr(self, k)}
            for k, v in new_instance_funcs.items():
                # Check to make sure v is not already a bound method of self. This can happen if the user sets a method
                #   of self to another previously bound method of self while in __init__
                if isinstance(v, MethodType) and getattr(self, v.__name__) == v:
                    continue

                setattr(self, k, MethodType(v, self))
                self.__instance_funcs__[k] = getattr(self, k)  # Update the instance funcs with the new function
            
            if remove_instance_funcs:
                del self.__instance_funcs__
        
        ret_cls.__init__ = insert_post  # Set this class's __init__ function to be the new one
        return paramspec_set_class_funcs(ret_cls)
    

class NormalizerState:
    """A class that contains information during a normalizer's normalization process"""

    orig_token = None
    """str: The current string token being normalized"""

    token = None
    """str: The current processed version of token if it has already been partially or fully normalized, or None if not"""

    token_type = None
    """str: The token type of the current token, see bincfg.normalization.base_tokenizer.Tokens"""

    token_idx = None
    """int: The index of the current token in 'line'"""

    line = None
    """List[Tuple[str, str, str]]): list of all TokenTuple's in this current line.
       TokenTuple = (token_type [from `bincfg.normalization.base_tokenizer.Tokens` enum], new_token_string, original_token_string)"""
    
    normalized_lines = None
    """List[str]): list of all currently normalized lines/tokens (depending on self.tokenization_level)"""
    
    raw_strings = None
    """List[str]: list of all of the raw strings passed to the current .normalize() call"""
    
    match_instruction_address = None
    """bool: whether or not we are matching instruction addresses at the beginning of assembly lines. This is very likely always True"""
    
    newline_tup = None
    """Optional[Tuple[str, str]]: the newline tuple being used (token_type [probably Tokens.NEWLINE], token_string), or None if not using"""
    
    cfg = None
    """Optional[bincfg.CFG]: the CFG that this token's basic block belongs to, or None if not using"""

    block = None
    """Optional[bincfg.CFGBasicBlock]: the CFGBasicBlock that this token belongs to, or None if not using"""

    memory_start = None
    """Optional[int]: the index of the start of the current memory expression, or None if we are not in a memory expression currently"""

    disinfo_json = None
    """Optional[JSONObject]: the parsed json from a disinfo object"""

    handlers = None
    """Dict[str, Callable[[NormalizerState], Union[str, None]]]: dictionary of current token handler functions"""

    kwargs = None
    """Dict: dictionary of extra kwargs for use in tokenization, or child classes"""

    def __init__(self, **kwargs):
        self.set(**kwargs)
    
    def set(self, **kwargs):
        """Sets the given kwargs on this object's attribute dictionary"""
        for k, v in kwargs.items():
            setattr(self, k, v)
    
    def copy(self):
        """Returns a copy of this state, but doesn't copy `cfg` or `block`"""
        return NormalizerState(**{k: (copy.deepcopy(v) if k not in ['cfg', 'block'] else v) for k, v in self.__dict__.items()})
    
    def copy_set(self, **kwargs):
        """Copies this state, then updates all the given parameters"""
        ret = self.copy()
        ret.set(**kwargs)
        return ret
    
    def __getitem__(self, key):
        """Allows access like dictionary keys"""
        if key in dir(self):
            return getattr(self, key)
        raise KeyError(key)
    
    def __setitem__(self, key, value):
        """Allows access like dictionary keys"""
        if key in dir(self):
            setattr(self, key, value)
        raise KeyError(key)
    
    def __str__(self):
        return repr(self.__dict__)


class BaseNormalizer(metaclass=MetaNorm):
    """A base class for a normalization method. 
    
    This should be subclassed once for each new instruction set to create a base normalizer for that instruction set
    that performs a default 'unnormalized' normalization

    There are three types of functions that are intended to be overridden when needed:

    1. Token handlers: these functions will start with 'handle' and are used to handle either single tokens, or small
       groups of similar tokens (EG: memory expressions). They should accept both self and 'state' as inputs (see
       `bincfg.normalization.base_normalizer.NormalizerState`) and can return either a token which will be added to the
       end of the current line, or None to not add any token post-calling.
    2. Opcode handlers: these functions will start with 'opcode' and are used to handle specific opcodes (not the
       'opcode' token in general, only specific ones like 'call' or 'jump' opcodes). They should accept both self and 
       'state' as inputs (See ``bincfg.normalization.base_normalizer.NormalizerState``) and can return either the integer 
       index of the next token that should be checked (IE: "we have handled all tokens up to but not including this index"),
       or None to indicate the previously mentioned index is just one after the opcode. These operate directly on the
       state's current '.line' attribute. These are expected to be called only after the entire current line has finished
       being parsed and normalized. New opcode handlers can be added with self.register_opcode_handler()
    3. Administrative functions: these functions perform different administrative operations before, during, or after
       normalizing the individual tokens. Some examples include:

       - 'finalize_instruction': used as a post-processing function once an instruction has finished being normalized to perform
         extra processing to the line, apply opcode handlers, stringify the line, update the normalizer state
       - 'hash_token': hashes a fully processed string token (if self.anonymize_tokens=True)
       - 'stringify_line': takes the current line of token tuples and converts into strings based on self.tokenization_level
    
    Disassembler Information:

    Extra information from the disassembler can be inserted into the lines within angle brackets "<>" (see
    :func:`~bincfg.normalization.base_tokenizer.BaseTokenizer` for info on how this can be tokenized). This disassembler
    info will be treated as a single token, and passed to the `self.handle_disassembler_info` function. By default, the
    normalizer will check for the following in order

        1. Valid JSON. If the data inside of the angle brackets is valid JSON, then it will be parsed into a JSON object.
           This JSON object will be inserted into the `state.disinfo_json` attribute in the normalizer state. There are
           a few special cases for this JSON data that have special effects by default:

            * If this object is an integer, we will attempt to insert it into a previous immediate value like in #2 below
            * If this is a string, we will always insert it as a string literal like in #3 below
            * If this is a dictionary, there are a few special keys that one can use:

                - 'immediate': value should be an integer. We will attempt to insert value into a previous immediate
                  value like in #2 below
                - 'insert': this value will be inserted into the string. If it is already a string, it is left as-is. If
                  not a string, then we call repr() on it to convert it into a string. Insertion actions depend on whether 
                  or not the key 'insert_type' is present. 
                  
                  If not present, this value will first be tokenized/normalized by this normalizer and that
                  value + token type will be inserted. Should that fail, then the value will be inserted as a string
                  literal WITHOUT processing it as a string literal token. 
                  
                  If the 'insert_type' key is present, then it can be one of two values:

                    * String token_type: the value will be handled as if it is of this token type, no matter what the
                      value actually is, then it will be inserted (assuming that token handler did not return None)
                    * False (the JSON object, not the string): the value will be immediately inserted as a string literal
                      WITHOUT processing it as a string literal token
                
                - 'insert_type': Determines the token type for an 'insert' key value. Ignored if the 'insert' key is not
                  present. See the 'insert' key for more info
            
        2. Otherwise, if the disassembler info token starts with an immediate value within the angle brackets, and there
           is an immediate value token immediately preceeding them (ignoring spacing tokens), this will replace said 
           immediate value token with the immediate value found within the disassembler info. The inserted value will
           first be handled by the appropriate handler for Token.IMMEDIATE token types.
           EG: "add rax 0xffff <-1>"  ->  "add rax -1"
        3. Otherwise, if the disassembler info token starts with a string literal, this will insert that string literal
           right where it appears (and, that string literal will be handled with `self.handle_string_literal`). The inserted
           value will first be handled by the appropriate handler for Token.STRING_LITERAL token types.
    
    The disassembler tokens themselves are always ignored by default.
    
    NOTE: immediates and string literals must match those found in ``bincfg.normalization.norm_utils`` (`RE_IMMEDIATE`
    and `RE_STRING_LITERAL`). The disassembler info does not take into account the regex's used to parse immediates
    and string literals for the specific normalizer.
       
    
    Parameters
    ----------
    tokenizer: `Tokenizer`
        the tokenizer to use
    token_handlers: `Optional[Dict[str, Callable[[NormalizerState], Union[None, str]]]]`
        optional dictionary mapping string token types to functions to handle those tokens. These will override any
        token handlers that are used by default (IE: all of the `self.handle_*` functions). Functions should take one
        arg (the current normalizer state) as input and return either the next string token to add to the current line,
        or None to not add anything. This is useful for adding more methods to handle new token types that are not builtin.
    token_sep: `Optional[str]`
        the string to use to separate each token in returned instruction lines. Only used if tokenization_level is 
        'instruction'. If None, then a default value will be used (' ' for unnormalized using BaseNormalizer(), '_' 
        for everything else)
    tokenization_level: `Optional[Union[TokenizationLevel, str]]`
        the tokenization level to use for return values. Can be a string, or a ``TokenizationLevel`` type. Strings can be:

            - 'op': tokenized at the opcode/operand level. Will insert a 'INSTRUCTION_START' token at the beginning of
              each instruction line
            - 'inst'/'instruction': tokenized at the instruction level. All tokens in each instruction line are joined
              together using token_sep to construct the final token
            - 'auto': pick the default value for this normalization technique

    anonymize_tokens: `bool`
        if True, then tokens will be annonymized by taking their 4-byte shake_128 hash. Why does this exist? Bureaucracy.
    """

    DEFAULT_TOKENIZATION_LEVEL = TokenizationLevel.INSTRUCTION
    """The default tokenization level used for this normalizer"""

    renormalizable = False
    """Whether or not this normalization method can be renormalized later by other normalization methods"""

    tokenizer = None
    """The tokenizer used for this normalizer"""

    token_sep = None
    """The separator string used for this normalizer
    
    Will default to ' '
    """

    tokenization_level = TokenizationLevel.AUTO
    """The tokenization level to use for this normalizer"""

    def __init__(self, tokenizer, token_handlers=None, token_sep=' ', tokenization_level=TokenizationLevel.AUTO, anonymize_tokens=False):
        self.tokenizer, self.token_sep, self.anonymize_tokens = tokenizer, token_sep, anonymize_tokens
        self.token_handlers = {} if token_handlers is None else token_handlers
        self.tokenization_level = parse_tokenization_level(tokenization_level, self.DEFAULT_TOKENIZATION_LEVEL)

        self.opcode_handlers = []
    
    def register_opcode_handler(self, op_regex, func_or_str_name):
        """Registers an opcode handler for this normalizer

        Adds the given `op_regex` as an opcode to handle during self._handle_instruction() along with the given function
        to call with token/cfg arguments. `op_regex` can be either a compiled regex expression, or a string which
        will be compiled into a regex expression. `func_or_str_name` can either be a callable, or a string. If it's
        a string, then that attribute will be looked up on this normalizer dynamically to find the function to use.

        Notes for registering opcode handlers:

            1. passing instance method functions converts them to strings automatically
            2. passing lambda's or inner functions (not at global scope) would not be able to be pickled
            3. opcodes will be matched in the order they were passed in

        Args:
            op_regex (Union[str, Pattern]): a string or compiled regex
            func_or_str_name (Union[Callable, str]): the function to call with token/cfg arguments when an opcode 
                matches op_regex, or a string name of a callable attribute of this normalizer to be looked up dynamically
        """
        op_regex = re.compile(op_regex) if isinstance(op_regex, str) else op_regex

        if not isinstance(func_or_str_name, str):
            # Check it is callable
            if not callable(func_or_str_name):
                raise TypeError("fun_or_str_name must be str or callable, not '%s'" % type(func_or_str_name))

            # Check if the passed function is an instance method of this normalization method class specifically
            # Have to check if func_or_str_name has a __name__ attribute first since they could sometimes be _LOF classes
            if hasattr(func_or_str_name, '__name__') and hasattr(self.__class__, func_or_str_name.__name__):
                func_or_str_name = func_or_str_name.__name__

        self.opcode_handlers.append((op_regex, func_or_str_name))
    
    def tokenize(self, *strings, newline_tup=_USE_DEFAULT_NT, match_instruction_address=True, **kwargs):
        """Tokenizes the given strings using this normalizer's tokenizer

        See the docs for :func:`~bincfg.normalization.base_tokenizer.BaseTokenizer` for more info on how tokenization
        works, how to create subclasses, etc.

        Args:
            strings (str): arbitrary number of strings to tokenize.
            newline_tup (Optional[Tuple[str, str]]): the tuple to insert inbetween each passed string, or None to not 
                insert anything. Defaults to `self.__class__.DEFAULT_NEWLINE_TUPLE`.
            match_instruction_address (bool, optional): if True, will match instruction addresses. If there is an immediate
                value at the start of a line (IE: start of a string in `strings`, or immediately after a Tokens.NEWLINE
                or Tokens.INSTRUCTION_START [ignoring any Tokens.SPACING]), then that token will be converted into a
                Tokens.INSTRUCTION_ADDRESS token. If there is a Tokens.COLON immediately after that token (again, ignoring
                any Tokens.SPACING), then that first Tokens.COLON match will be appended (along with any inbetween Tokens.SPACING)
                to that Tokens.INSTRUCTION_ADDRESS token. For example, using the x86 tokenization scheme:

                    - "0x1234: add rax rax" -> [(Tokens.INSTRUCTION_ADDRESS, '0x1234:'), ...]
                    - "  0x1234     : add rax rax" -> [(Tokens.SPACING, '  '), (Tokens.INSTRUCTION_ADDRESS, '0x1234     :'), ...]
                    - "0x1234 add rax rax" -> [(Tokens.INSTRUCTION_ADDRESS, '0x1234'), ...]
                
            kwargs (Any): extra kwargs to store in the tokenizer state, for use in child classes

        Returns:
            List[Tuple[str, str]]: list of (token_type, token) tuples
        """
        return self.tokenizer(*strings, newline_tup=newline_tup, match_instruction_address=match_instruction_address, **kwargs)

    def normalize(self, *strings, cfg=None, block=None, newline_tup=_USE_DEFAULT_NT, match_instruction_address=True, **kwargs):
        """Normalizes the given iterable of strings.

        Args:
            strings (str): arbitrary number of strings to normalize
            cfg (Union[CFG, MemCFG], optional): either a ``CFG`` or ``MemCFG`` object that these lines occur 
                in. Used for determining function calls to self, internal functions, and external functions. If not 
                passed, then these will not be used. Defaults to None.
            block (Union[CFGBasicBlock, int], optional): either a ``CFGBasicBlock`` or integer block_idx in a ``MemCFG``
                object. Used for determining function calls to self, internal functions, and external functions. If not 
                passed, then these will not be used. Defaults to None.
            newline_tup (Tuple[str, str], optional): the tuple to insert inbetween each passed string, or None to not 
                insert anything. Defaults to self.tokenizer.DEFAULT_NEWLINE_TUPLE
            match_instruction_address (bool, optional): if True, will match instruction addresses. If there is an immediate
                value at the start of a line (IE: start of a string in `strings`, or immediately after a Tokens.NEWLINE
                or Tokens.INSTRUCTION_START [ignoring any Tokens.SPACING]), then that token will be converted into a
                Tokens.INSTRUCTION_ADDRESS token. If there is a Tokens.COLON immediately after that token (again, ignoring
                any Tokens.SPACING), then that first Tokens.COLON match will be appended (along with any inbetween Tokens.SPACING)
                to that Tokens.INSTRUCTION_ADDRESS token. For example, using the x86 tokenization scheme:

                    - "0x1234: add rax rax" -> [(Tokens.INSTRUCTION_ADDRESS, '0x1234:'), ...]
                    - "  0x1234     : add rax rax" -> [(Tokens.SPACING, '  '), (Tokens.INSTRUCTION_ADDRESS, '0x1234     :'), ...]
                    - "0x1234 add rax rax" -> [(Tokens.INSTRUCTION_ADDRESS, '0x1234'), ...]
                
            kwargs (Any): extra kwargs to pass along to tokenization method, and to store in normalizer state

        Returns:
            List[str]: a list of normalized string instruction lines
        """
        # If no strings were passed, return empty list
        if len(strings) == 0:
            return []
        
        # Check if the first string is an instruction start. If so, we are normalizing an already-normalized string that
        #   was normalized with tokenization_level='op'. Combine all the strings together, assuming the instruction start
        #   tokens are inbetween all of the instructions
        if strings[0] == INSTRUCTION_START_TOKEN:
            newline_tup = None
        
        newline_tup = newline_tup if newline_tup is not _USE_DEFAULT_NT else self.tokenizer.DEFAULT_NEWLINE_TUPLE
        
        # Get the current mapping of token types to their handler functions
        handler_mapping = {

            Tokens.INSTRUCTION_ADDRESS: self.handle_instruction_address,
            #Tokens.INSTRUCTION_START: Will occurr, but we handle it seperately,
            #Tokens.SPLIT_IMMEDIATE: Should never occurr,
            Tokens.DISASSEMBLER_INFO: self.handle_disassembler_info,
            Tokens.NEWLINE: self.handle_newline,
            Tokens.SPACING: self.handle_spacing,

            Tokens.OPEN_BRACKET: self.handle_all_symbols,
            Tokens.CLOSE_BRACKET: self.handle_all_symbols,
            Tokens.PLUS_SIGN: self.handle_all_symbols,
            Tokens.TIMES_SIGN: self.handle_all_symbols,
            Tokens.COLON: self.handle_all_symbols,

            Tokens.INSTRUCTION_PREFIX: self.handle_instruction_prefix,
            Tokens.OPCODE: self.handle_opcode,
            Tokens.REGISTER: self.handle_register,
            Tokens.IMMEDIATE: self.handle_immediate,

            Tokens.MEMORY_SIZE: self.handle_memory_size,
            #Tokens.MEMORY_EXPRESSION: doesn't occurr here, that's for subclasses
            Tokens.BRANCH_PREDICTION: self.handle_branch_prediction,
            Tokens.STRING_LITERAL: self.handle_string_literal,

            Tokens.MISMATCH: self.handle_mismatch,
        }
        handler_mapping.update(self.token_handlers)

        # Initialize the current state that gets passed around to function calls
        state = NormalizerState(cfg=cfg, block=block, newline_tup=newline_tup, match_instruction_address=match_instruction_address,
                                normalized_lines=[], line=[], raw_strings=strings, kwargs=kwargs, handlers=handler_mapping)
        
        for token_type, old_token in self.tokenize(*strings, newline_tup=newline_tup, match_instruction_address=match_instruction_address, **kwargs):
            state.token_type, state.orig_token, state.token = token_type, old_token, old_token
            
            # Handle this current token
            self._handle_token(state)
            
            # If this was a newline token or instruction start token, call our line handler
            if state.token_type in [Tokens.NEWLINE, Tokens.INSTRUCTION_START]:
                self.finalize_instruction(state)
        
        self.finalize_instruction(state)

        # If we currently have no lines, then insert an empty string
        if len(state.normalized_lines) == 0:
            state.normalized_lines.append("")

        # If we are anonymizing the tokens, do that now
        if self.anonymize_tokens:
            for i, t in enumerate(state.normalized_lines):
                state.normalized_lines[i] = self.hash_token(t)
        
        return state.normalized_lines
    
    def _handle_token(self, state, insert_token=True):
        """Handles a single token of the given token_type. Returns the token no matter what"""
        state.token = state.handlers[state.token_type](state) if state.token_type in state.handlers \
            else None if state.token_type in [Tokens.INSTRUCTION_START] \
            else self.handle_unknown_token(state)
            
        # Add this (name, new_token, old_token) triplet to our list if new_token is not None
        insert_token_tup = (state.token_type, state.token, state.orig_token)
        if state.token is not None and insert_token:
            state.line.append(insert_token_tup)

        return insert_token_tup
    
    def handle_opcode(self, state):
        """Handles an opcode. Defaults to returning the original token

        NOTE: This should only be used to determine how all opcode strings are handled. For how to handle specific opcodes
        to give them different behaviors, see :func:`~bincfg.normalization.base_normalizer.BaseNormalizer.register_opcode_handler`

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return state.token
    
    def handle_all_symbols(self, state):
        """Handles symbols ('+', '[', ']', '*', ':'). Defaults to returning the original token

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return state.token
    
    def handle_memory_size(self, state):
        """Handles a memory size. Defaults to returning the original token

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return state.token
    
    def handle_register(self, state):
        """Handles a register. Defaults to returning the original token

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return state.token
    
    def handle_instruction_prefix(self, state):
        """Handles an instruction prefix. Defaults to returning the original token

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return state.token
    
    def handle_branch_prediction(self, state):
        """Handles a branch prediction. Defaults to returning the original token

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return state.token
    
    def handle_instruction_address(self, state):
        """Handles an instruction address. Defaults to ignoring these tokens

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return None
    
    def handle_spacing(self, state):
        """Handles spacing. Defaults to ignoring these tokens

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return None

    def handle_immediate(self, state):
        """Handles an immediate value. Defaults to converting into decimal

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return str(imm_to_int(state.token))
    
    def handle_newline(self, state):
        """Handles a newline token. Defaults to ignoring the token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return None
    
    def handle_disassembler_info(self, state):
        """Handles disassembler information

        See :func:`~bincfg.normalization.base_normalizer.BaseNormalizer` for more info on how disassembler info is parsed.

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        # Clear the disinfo_json attribute
        state.disinfo_json = None

        # Get the actual token
        disinfo = state.token[len(DISINFO_START):-len(DISINFO_END)]

        # Functions for inserting immediate values and string literals
        def _insert_imm(val):
            idx = scan_for_token(state.line, type=[Tokens.IMMEDIATE], stop_unmatched=True, ignore_type=[Tokens.SPACING], start=-1, increment=-1)
            if idx is not None:
                state.line[idx] = self._handle_token(state.copy_set(token_type=Tokens.IMMEDIATE, token=val), insert_token=False)
        def _insert_str(val):
            state.line.append(self._handle_token(state.copy_set(token_type=Tokens.STRING_LITERAL, token=val), insert_token=False))

        # Attempt to parse as a JSON object
        parsed_json = parse_disinfo_json(disinfo)
        if parsed_json is not None:
            state.disinfo_json = parsed_json

            # If this is an immediate or a string, apply those
            if isinstance(parsed_json, int):
                _insert_imm(parsed_json)
            elif isinstance(parsed_json, str):
                _insert_str('"' + parsed_json + '"')  # JSON can only handle double quotes
            
            # If this is a dictionary with special keys, handle those
            elif isinstance(parsed_json, dict):

                if 'immediate' in parsed_json:
                    _insert_imm(parsed_json['immediate'])
                elif 'insert' in parsed_json:
                    ins_str = parsed_json['insert'] if isinstance(parsed_json['insert'], str) else repr(parsed_json['insert'])

                    if 'insert_type' in parsed_json:
                        if isinstance(parsed_json['insert_type'], bool) and not parsed_json['insert_type']:
                            insert = (Tokens.STRING_LITERAL, ins_str, ins_str)
                        else:
                            insert = self._handle_token(state.copy_set(token=ins_str, token_type=parsed_json['insert_type'], old_token=ins_str))
                    else:
                        try:
                            tokens = self.tokenize(ins_str, newline_tup=None, match_instruction_address=False, **state['kwargs'])
                            if len(tokens) != 1:
                                raise ValueError
                            insert = tokens[0] + (ins_str,)
                        except:
                            insert = (Tokens.STRING_LITERAL, ins_str, ins_str)
                    
                    if insert[1] is not None:
                        state.line.append(insert)

        else:
            # Check for an immediate value at the start
            mo = RE_DISINFO_IMM.fullmatch(disinfo)
            if mo is not None:
                _insert_imm(mo.groups()[0])

            # Finally, check for a string literal
            mo = RE_DISINFO_STR.fullmatch(disinfo)
            if mo is not None:
                _insert_str(mo.groups()[0])
            
        return None
    
    def handle_string_literal(self, state):
        """Handles string literals. Defaults to returning the original token

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        return state.token

    def handle_mismatch(self, state):
        """What to do when the normalizaion method finds a token mismatch (in case they were ignored in the tokenizer)

        Defaults to raising a TokenMismatchError()

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        
        Raises:
            TokenMismatchError: always
        """
        raise TokenMismatchError("Mismatched token %s found during normalization!" % repr(state.token))
    
    def handle_unknown_token(self, state):
        """Handles an unknown token. Defaults to raising an UnknownTokenError

        Should return either the token to add to the current line, or None to not add any token

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        
        Raises:
            UnknownTokenError: always
        """
        raise UnknownTokenError("Unknown token type %s" % repr(state.token_type))
    
    def finalize_instruction(self, state):
        """Handles an entire instruction once reaching a new line

        If overridden, should at the very least:

            - call all the registered opcode handlers for each known opcode token (while updating token_type/token/token_idx)
            - stringify the line
            - add new line to state.normalized_lines and clear state.line
        
        By default, each opcode handler is expected to take in the current state, and return either the integer index
        of the next token that should be checked (IE: "we have handled all tokens up to but not including this index"),
        or None to indicate the previously mentioned index is just one after the opcode

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
        """
        # If this is an empty line, just return
        if len(state.line) == 0:
            return
        
        # Handle all of the opcodes
        idx = 0
        while idx < len(state.line):
            
            # Check for any handled opcodes
            if state.line[idx][0] == Tokens.OPCODE:
                state.token_type, state.token, _ = state.line[idx]
                state.token_idx = idx

                for regex, func in self.opcode_handlers:
                    if regex.fullmatch(state.token) is not None:
                        # Check for string name to lookup on self
                        if isinstance(func, str):
                            func = getattr(self, func)

                        new_idx = func(state)
                        idx = (new_idx - 1) if new_idx is not None else idx
                        break
            
            idx += 1
        
        # Stringify this line, add it to our normalized lines, and clear state.line
        sl = self.stringify_line(state)
        state.normalized_lines += [sl] if isinstance(sl, str) else list(sl)
        state.line.clear()
    
    def hash_token(self, token):
        """Hashes tokens during annonymization

        By default, converts each individual token into its 4-byte shake_128 hash

        Args:
            token (str): the string token to hash
        
        Returns:
            str: the 4-byte shake_128 hash of the given token
        """
        hasher = hashlib.shake_128()
        hasher.update(token.encode('utf-8'))
        return hasher.hexdigest(4)

    def stringify_line(self, state):
        """Converts the current line into a list of final normalized string tokens and returns that list

        Args:
            state (NormalizerState): dictionary of current state information. See ``bincfg.normalization.base_normalizer.NormalizerState``
            
        Returns:
            List[str]: a list of tokens to add to state.normalized_lines
        """
        tokens = [t for n, t, _ in state.line]
        if len(tokens) == 0:
            return []
        
        if self.tokenization_level == TokenizationLevel.INSTRUCTION:
            return [self.token_sep.join(tokens)]
        elif self.tokenization_level == TokenizationLevel.OPCODE:
            return [INSTRUCTION_START_TOKEN] + tokens
        else:
            raise ValueError("Unknown TokenizationLevel: %s" % self.tokenization_level)
    
    def __call__(self, *strings, cfg=None, block=None, newline_tup=_USE_DEFAULT_NT, match_instruction_address=True, **kwargs):
        """Normalizes the given iterable of strings.

        Args:
            strings (str): arbitrary number of strings to normalize
            cfg (Union[CFG, MemCFG], optional): either a ``CFG`` or ``MemCFG`` object that these lines occur 
                in. Used for determining function calls to self, internal functions, and external functions. If not 
                passed, then these will not be used. Defaults to None.
            block (Union[CFGBasicBlock, int], optional): either a ``CFGBasicBlock`` or integer block_idx in a ``MemCFG``
                object. Used for determining function calls to self, internal functions, and external functions. If not 
                passed, then these will not be used. Defaults to None.
            newline_tup (Tuple[str, str], optional): the tuple to insert inbetween each passed string, or None to not 
                insert anything. Defaults to self.tokenizer.DEFAULT_NEWLINE_TUPLE
            match_instruction_address (bool, optional): if True, will match instruction addresses. If there is an immediate
                value at the start of a line (IE: start of a string in `strings`, or immediately after a Tokens.NEWLINE
                or Tokens.INSTRUCTION_START [ignoring any Tokens.SPACING]), then that token will be converted into a
                Tokens.INSTRUCTION_ADDRESS token. If there is a Tokens.COLON immediately after that token (again, ignoring
                any Tokens.SPACING), then that first Tokens.COLON match will be appended (along with any inbetween Tokens.SPACING)
                to that Tokens.INSTRUCTION_ADDRESS token. For example, using the x86 tokenization scheme:

                    - "0x1234: add rax rax" -> [(Tokens.INSTRUCTION_ADDRESS, '0x1234:'), ...]
                    - "  0x1234     : add rax rax" -> [(Tokens.SPACING, '  '), (Tokens.INSTRUCTION_ADDRESS, '0x1234     :'), ...]
                    - "0x1234 add rax rax" -> [(Tokens.INSTRUCTION_ADDRESS, '0x1234'), ...]
                
            kwargs (Any): extra kwargs to pass along to tokenization method, and to store in normalizer state

        Returns:
            List[str]: a list of normalized string instruction lines
        """
        return self.normalize(*strings, cfg=cfg, block=block, newline_tup=newline_tup, match_instruction_address=match_instruction_address, **kwargs)

    def __eq__(self, other):
        """Checks equality between this normalizer and another. 
        
        Defaults to checking if class types, tokenizers, and tokenization_level are the same. Future children should 
            also check any kwargs.
        """
        return type(self) == type(other) and eq_obj([r for r, _ in self.opcode_handlers], [r for r, _ in other.opcode_handlers]) \
            and all(eq_obj(self, other, selector=s) for s in ['tokenizer', 'tokenization_level', 'anonymize_tokens', 'renormalizable', 'token_sep', 'token_handlers'])
    
    def __hash__(self):
        return hash_obj([type(self).__name__, [r for r, _ in self.opcode_handlers], self.tokenizer, self.tokenization_level.name,
                         self.anonymize_tokens, self.renormalizable, self.token_sep, self.token_handlers], return_int=True)
    
    def __repr__(self) -> str:
        _num_str_chars = 30
        def _clean_str(o):
            s = repr(o)
            if len(s) > _num_str_chars:
                return o.__class__.__name__ + "(...)"
            return s
        def _clean_kwarg(k, v):
            if k == 'tokenizer':
                ret = self.tokenizer
            elif k == 'token_sep':
                ret = self.token_sep
            elif k == 'tokenization_level':
                ret = self.tokenization_level.name.lower()
            else:
                ret = v
            return _clean_str(ret)

        args_kwargs_str = [('%s=%s' % (k, _clean_str(v))) for k, v in self.__savedparams__['__init__']['args'].items()] + \
            [('%s=%s' % (k, _clean_kwarg(k, v))) for k, v in self.__savedparams__['__init__']['kwargs'].items()]
        return self.__class__.__name__ + "(" + ', '.join(args_kwargs_str) + ")"
    
    def __str__(self) -> str:
        return self.__class__.__name__.lower() + (('_op' if self.tokenization_level == TokenizationLevel.OPCODE else '_inst') if self.tokenization_level != self.DEFAULT_TOKENIZATION_LEVEL else '')


# Libc function names gathered from: https://www.gnu.org/software/libc/manual/html_node/Function-Index.html
# Code used to generate these from raw copy/pasted website data:
"""
import re

libc_funcs = set()
for l in s.split('\n'):
    mo = re.fullmatch(r'[ \t\n]*([0-9a-zA-Z_*]+):.*', l)

    if mo is not None:
        libc_funcs.add(mo.groups()[0])

with open('./libc_func_names.txt', 'w') as f:
    for n in sorted(list(libc_funcs)):
        f.write(n + '\n')
"""
with open(os.path.join(os.path.dirname(__file__), "libc_func_names.txt"), 'r') as f:
    LIBC_FUNCTION_NAMES = set([n.replace('\n', '') for n in f.readlines() if not re.fullmatch(r'[ \t\n]*', n)])