# Copyright (c) 2020 The Regents of the University of Michigan
# All rights reserved.
# This software is licensed under the BSD 3-Clause License.
"""Implement the SyncedCollection class."""
from abc import abstractmethod
from collections import defaultdict
from collections.abc import Collection
from inspect import isabstract
from threading import RLock
from typing import Any, Callable, DefaultDict, List

from ..numpy_utils import _convert_numpy
from ..utils import AbstractTypeResolver, _CounterContext, _NullContext

# Identifies types of SyncedCollection, which are the base type for this class.
_sc_resolver = AbstractTypeResolver(
    {
        "SYNCEDCOLLECTION": lambda obj: isinstance(obj, SyncedCollection),
    }
)


class _LoadAndSave:
    """A context manager for :class:`SyncedCollection` to wrap saving and loading.

    Any write operation on a synced collection must be preceded by a load and
    followed by a save. Moreover, additional logic may be required to handle
    other aspects of the synchronization, particularly the acquisition of thread
    locks. This class abstracts this concept, making it easy for subclasses to
    customize the behavior if needed (for instance, to introduce additional locks).
    """

    def __init__(self, collection):
        self._collection = collection

    def __enter__(self):
        self._collection._thread_lock.__enter__()
        self._collection._load()

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._collection._save()
        finally:
            self._collection._thread_lock.__exit__(exc_type, exc_val, exc_tb)


class SyncedCollection(Collection):
    """An abstract :class:`~collections.abc.Collection` type that is synced with a backend.

    This class extends :py:class:`collections.abc.Collection` and adds a number of abstract
    internal methods that must be implemented by its subclasses. These methods can be
    split into two groups of functions that are designed to be implemented by
    separate subtrees in the inheritance hierarchy that can then be composed:

        **Concrete Collection Types**

        These subclasses should implement the APIs for specific types of
        collections. For instance, a list-like :class:`SyncedCollection`
        should implement the standard methods for sequences. In addition, they
        must implement the following abstract methods defined by the
        :class:`SyncedCollection`:

        - :meth:`~.is_base_type`: Determines whether an object satisfies the
          semantics of the collection object a given :class:`SyncedCollection`
          is designed to mimic.
        - :meth:`~._to_base`: Converts a :class:`SyncedCollection` to its
          natural base type (e.g. a `list`).
        - :meth:`~._update`: Updates the :class:`SyncedCollection` to match the
          contents of the provided :py:class:`collections.abc.Collection`.
          After calling ``sc._update(c)``, we must have that ``sc == c``; however,
          since such updates are frequent when loading and saving data to a
          resource, :meth:`_update` should be implemented to minimize new object
          creation wherever possible.

        **Backend**

        These subclasses encode the process by which in-memory data is
        converted into a representation suitable for a particular backend. For
        instance, a JSON backend should know how to save a Python object into a
        JSON-encoded file and then read that object back.

        - :meth:`~._load_from_resource`: Loads data from the underlying
          resource and returns it in an object satisfying :meth:`~.is_base_type`.
        - :meth:`~._save_to_resource`: Stores data to the underlying resource.
        - :attr:`~._backend`: A unique string identifier for the resource backend.

    Since these functionalities are effectively completely orthogonal, members of
    a given group should be interchangeable. For instance, a dict-like SyncedCollection
    can be combined equally easily with JSON, MongoDB, or SQL backends.

    To fully support the restrictions and requirements of particular backends,
    arbitrary validators may be added to different subclasses. Validators are
    callables that accept different data types as input and raise Exceptions if the
    data does not conform to the requirements of a particular backend. For
    example, a JSON validator would raise Exceptions if it detected non-string
    keys in a dict. Validators should have no side effects.

    .. note::

        Typically, a synced collection will be initialized with resource information,
        and data will be pulled from that resource. However, initializing with
        both data and resource information is a valid use case. In this case, the
        initial data will be validated by the standard validators, however, it
        will not be checked against the contents stored in the synced resource and
        is assumed to be consistent. This constructor pattern can be useful to
        avoid unnecessary resource accesses.

    **Thread safety**

    Whether or not SyncedCollection objects are thread-safe depends on the
    implementation of the backend. Thread-safety of SyncedCollection objects
    is predicated on backends providing an atomic write operation. All concrete
    collection types use mutexes to guard against concurrent write operations,
    while allowing read operations to happen freely. The validity of this mode
    of access depends on the write operations of a SyncedCollection being
    atomic, specifically the `:meth:`~._save_to_resource` method.

    Backends that support multithreaded execution will have multithreaded
    support turned on by default. This support can be enabled or disabled using
    the :meth:`enable_multithreading` and :meth:`disable_multithreading`
    methods.


    Parameters
    ----------
    parent : SyncedCollection, optional
        If provided, the collection within which this collection is nested
        (Default value = None).
        A parent instance of :class:`SyncedCollection` or ``None``. If ``None``,
        the collection owns its own data, otherwise it is nested within its
        parent. Every :class:`SyncedCollection` either owns its own data, or has
        a parent (Default value = None).

    """

    registry: DefaultDict[str, List[Any]] = defaultdict(list)
    _validators: List[Callable] = []
    # Backends that support threading should modify this flag.
    _supports_threading: bool = False
    _LoadSaveType = _LoadAndSave

    def __init__(self, parent=None, *args, **kwargs):
        # Nested collections need to know their root collection, which is
        # responsible for all synchronization, and therefore all the associated
        # context managers are also stored from the root.
        if parent is not None:
            self._root = parent._root if parent._root is not None else parent
        else:
            self._root = parent
        self._suspend_sync = (
            _CounterContext() if self._root is None else self._root._suspend_sync
        )
        self._load_and_save = (
            type(self)._LoadSaveType(self)
            if self._root is None
            else self._root._load_and_save
        )
        if type(self)._supports_threading:
            type(self)._locks[self._lock_id] = RLock()

    @classmethod
    def __init_subclass__(cls):
        """Register and enable validation in subclasses.

        All subclasses are given a ``_validators`` list so that separate sets of
        validators can be registered to different types of synced collections. Concrete
        subclasses (those that have all methods implemented, i.e. that are associated
        with both a specific backend and a concrete data type) are also recorded in
        an internal registry that is used to convert data from some collection-like
        object into a :class:`SyncedCollection`.
        """
        # The Python data model promises that __init_subclass__ will be called
        # after the class namespace is fully defined, so at this point we know
        # whether we have a concrete subclass or not.
        if not isabstract(cls):
            SyncedCollection.registry[cls._backend].append(cls)
        cls._validators = []

        # Monkey-patch subclasses that support locking.
        if cls._supports_threading:
            cls._locks = {}
            cls.enable_multithreading()
        else:
            cls.disable_multithreading()

    @classmethod
    def enable_multithreading(cls):
        """Enable safety checks and thread locks required for thread safety.

        Support for multithreaded execution can be disabled by calling
        :meth:`~.disable_multithreading`; calling this method reverses that.

        """
        if cls._supports_threading:

            @property
            def _thread_lock(self):
                """Get the lock specific to this collection.

                Since locks support the context manager protocol, this method
                can typically be invoked directly as part of a ``with`` statement.
                """
                return type(self)._locks[self._lock_id]

            cls._thread_lock = _thread_lock
            cls._threading_support_is_active = True
        else:
            raise ValueError("This class does not support multithreaded execution.")

    @classmethod
    def disable_multithreading(cls):
        """Disable all safety checks and thread locks required for thread safety.

        The mutex locks required to enable multithreading introduce nontrivial performance
        costs, so they can be disabled for classes that support it.

        """
        cls._thread_lock = _NullContext()
        cls._threading_support_is_active = False

    @property
    def validators(self):
        """List[Callable]: The validators that will be applied.

        Validators are inherited from all parents of a class.
        """
        validators = []
        # Classes inherit the validators of their parent classes.
        for base_cls in type(self).__mro__:
            if hasattr(base_cls, "_validators"):
                validators.extend(
                    [v for v in base_cls._validators if v not in validators]
                )
        return validators

    @property
    def _lock_id(self):
        raise NotImplementedError(
            "Backends must implement the _lock_id property to support multithreaded "
            "execution. This property should return a hashable unique identifier for "
            "all collections that will be used to maintain a resource-specific "
            "set of locks."
        )

    @classmethod
    def add_validator(cls, *args):
        r"""Register a validator to this class.

        Parameters
        ----------
        \*args : List[Callable]
            Validator(s) to register.

        """
        cls._validators.extend([v for v in args if v not in cls._validators])

    @property
    @abstractmethod
    def _backend(self):
        """str: The backend associated with a given collection.

        This property is abstract to enforce that subclasses implement it.
        Since it's only internal, subclasses can safely override it with just a
        raw attribute; this property just serves as a way to enforce the
        abstract API for subclasses.
        """
        pass

    @classmethod
    def _from_base(cls, data, **kwargs):
        r"""Dynamically resolve the type of object to the corresponding synced collection.

        Parameters
        ----------
        data : Collection
            Data to be converted from base type.
        \*\*kwargs
            Any keyword arguments to pass to the collection constructor.

        Returns
        -------
        Collection
            Synced object of corresponding base type.

        Notes
        -----
        This method relies on the internal registry of subclasses populated by
        :meth:`~.__init_subclass__` and the :meth:`is_base_type` method to
        determine the subclass with the appropriate backend and data type. Once
        an appropriate type is determined, that class's constructor is called.
        Since this method relies on the constructor and other methods, it can
        be concretely implemented here rather than requiring subclass
        implementations.

        """
        for base_cls in SyncedCollection.registry[cls._backend]:
            if base_cls.is_base_type(data):
                return base_cls(data=data, **kwargs)
        return _convert_numpy(data)

    @abstractmethod
    def _to_base(self):
        """Dynamically resolve the synced collection to the corresponding base type.

        This method should not load the data from the underlying resource, it
        should simply converts the current in-memory representation of a synced
        collection to its naturally corresponding unsynced collection type.

        Returns
        -------
        Collection
            An equivalent unsynced collection satisfying :meth:`is_base_type`.

        """
        pass

    @classmethod
    @abstractmethod
    def is_base_type(cls, data):
        """Check whether data is of the same base type (such as list or dict) as this class.

        Parameters
        ----------
        data : Any
            The input data to test.

        Returns
        -------
        bool
            Whether or not the object can be converted into this synced collection type.

        """
        pass

    @abstractmethod
    def _load_from_resource(self):
        """Load data from underlying backend.

        This method must be implemented for each backend. Backends may choose
        to return ``None``, signaling that no modification should be performed
        on the data in memory. This mode is useful for backends where the underlying
        resource (e.g. a file) may not initially exist, but can be transparently
        created on save.

        Returns
        -------
        Collection or None
            An equivalent unsynced collection satisfying :meth:`is_base_type` that
            contains the data in the underlying resource (e.g. a file).

        """
        pass

    @abstractmethod
    def _save_to_resource(self):
        """Save data to the backend.

        This method must be implemented for each backend.
        """
        pass

    def _save(self):
        """Save the data to the backend.

        This method encodes the recursive logic required to handle the saving of
        nested collections. For a collection contained within another collection,
        only the parent is ever responsible for storing the data. This method
        handles the appropriate recursive calls, then farms out the actual writing
        to the abstract method :meth:`~._save_to_resource`.
        """
        if not self._suspend_sync:
            if self._root is None:
                self._save_to_resource()
            else:
                self._root._save()

    @abstractmethod
    def _update(self, data):
        """Update the in-memory representation to match the provided data.

        The purpose of this method is to update the SyncedCollection to match
        the data in the underlying resource.  The result of calling this method
        should be that ``self == data``. The reason that this method is
        necessary is that SyncedCollections can be nested, and nested
        collections must also be instances of SyncedCollection so that
        synchronization occurs even when nested structures are modified.
        Recreating the full nested structure every time data is reloaded from
        file is highly inefficient, so this method performs an in-place update
        that only changes entries that need to be changed.

        Parameters
        ----------
        data : Collection
            A collection satisfying :meth:`is_base_type`.

        """
        pass

    def _load(self):
        """Load the data from the backend.

        This method encodes the recursive logic required to handle the loading of
        nested collections. For a collection contained within another collection,
        only the root is ever responsible for loading the data. This method
        handles the appropriate recursive calls, then farms out the actual reading
        to the abstract method :meth:`~._load_from_resource`.
        """
        if not self._suspend_sync:
            if self._root is None:
                data = self._load_from_resource()
                with self._suspend_sync:
                    self._update(data)
            else:
                self._root._load()

    def _validate(self, data):
        """Validate the input data.

        Parameters
        ----------
        data : Collection
            An collection satisfying :meth:`is_base_type`.

        """
        for validator in self.validators:
            validator(data)

    # The following methods share a common implementation for
    # all data structures and regardless of backend.

    def __getitem__(self, key):
        self._load()
        return self._data[key]

    def __delitem__(self, key):
        with self._load_and_save:
            del self._data[key]

    def __iter__(self):
        self._load()
        return iter(self._data)

    def __len__(self):
        self._load()
        return len(self._data)

    def __call__(self):
        """Get an equivalent but unsynced object of the base data type.

        Returns
        -------
        Collection
            An equivalent unsynced collection satisfying :meth:`is_base_type`.

        """
        self._load()
        return self._to_base()

    def __eq__(self, other):
        self._load()
        if isinstance(other, type(self)):
            return self() == other()
        else:
            return self() == other

    def __repr__(self):
        self._load()
        return repr(self._data)

    def __str__(self):
        self._load()
        return str(self._data)
