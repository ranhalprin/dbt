import abc
from contextlib import contextmanager
from datetime import datetime
from typing import (
    Optional, Tuple, Callable, Container, FrozenSet, Type, Dict, Any, List,
    Mapping, Iterator, Union
)

import agate
import pytz

from dbt.exceptions import (
    raise_database_error, raise_compiler_error, invalid_type_error,
    get_relation_returned_multiple_results,
    InternalException, NotImplementedException, RuntimeException,
)
import dbt.flags

from dbt import deprecations
from dbt.clients.agate_helper import empty_table
from dbt.contracts.graph.compiled import CompileResultNode, CompiledSeedNode
from dbt.contracts.graph.manifest import Manifest
from dbt.contracts.graph.parsed import ParsedSeedNode
from dbt.node_types import NodeType
from dbt.logger import GLOBAL_LOGGER as logger
from dbt.utils import filter_null_values

from dbt.adapters.base.connections import BaseConnectionManager, Connection
from dbt.adapters.base.meta import AdapterMeta, available
from dbt.adapters.base.relation import (
    ComponentName, BaseRelation, InformationSchema
)
from dbt.adapters.base import Column as BaseColumn
from dbt.adapters.cache import RelationsCache


SeedModel = Union[ParsedSeedNode, CompiledSeedNode]


GET_CATALOG_MACRO_NAME = 'get_catalog'
FRESHNESS_MACRO_NAME = 'collect_freshness'


def _expect_row_value(key: str, row: agate.Row):
    if key not in row.keys():
        raise InternalException(
            'Got a row without "{}" column, columns: {}'
            .format(key, row.keys())
        )
    return row[key]


def _relations_filter_schemas(
    schemas: Container[str]
) -> Callable[[agate.Row], bool]:
    def test(row):
        referenced_schema = _expect_row_value('referenced_schema', row)
        dependent_schema = _expect_row_value('dependent_schema', row)
        # handle the null schema
        if referenced_schema is not None:
            referenced_schema = referenced_schema.lower()
        if dependent_schema is not None:
            dependent_schema = dependent_schema.lower()
        return referenced_schema in schemas or dependent_schema in schemas
    return test


def _catalog_filter_schemas(manifest: Manifest) -> Callable[[agate.Row], bool]:
    """Return a function that takes a row and decides if the row should be
    included in the catalog output.
    """
    schemas = frozenset((d.lower(), s.lower())
                        for d, s in manifest.get_used_schemas())

    def test(row: agate.Row) -> bool:
        table_database = _expect_row_value('table_database', row)
        table_schema = _expect_row_value('table_schema', row)
        # the schema may be present but None, which is not an error and should
        # be filtered out
        if table_schema is None:
            return False
        return (table_database.lower(), table_schema.lower()) in schemas
    return test


def _utc(
    dt: Optional[datetime], source: BaseRelation, field_name: str
) -> datetime:
    """If dt has a timezone, return a new datetime that's in UTC. Otherwise,
    assume the datetime is already for UTC and add the timezone.
    """
    if dt is None:
        raise raise_database_error(
            "Expected a non-null value when querying field '{}' of table "
            " {} but received value 'null' instead".format(
                field_name,
                source))

    elif not hasattr(dt, 'tzinfo'):
        raise raise_database_error(
            "Expected a timestamp value when querying field '{}' of table "
            "{} but received value of type '{}' instead".format(
                field_name,
                source,
                type(dt).__name__))

    elif dt.tzinfo:
        return dt.astimezone(pytz.UTC)
    else:
        return dt.replace(tzinfo=pytz.UTC)


def _relation_name(rel: Optional[BaseRelation]) -> str:
    if rel is None:
        return 'null relation'
    else:
        return str(rel)


class SchemaSearchMap(dict):
    """A utility class to keep track of what information_schema tables to
    search for what schemas
    """
    def add(self, relation: BaseRelation):
        key = relation.information_schema_only()
        if key not in self:
            self[key] = set()
        self[key].add(relation.schema.lower())

    def search(self):
        for information_schema_name, schemas in self.items():
            for schema in schemas:
                yield information_schema_name, schema

    def schemas_searched(self):
        result = set()
        for information_schema_name, schemas in self.items():
            result.update(
                (information_schema_name.database, schema)
                for schema in schemas
            )
        return result

    def flatten(self):
        new = self.__class__()

        # make sure we don't have duplicates
        seen = {r.database.lower() for r in self if r.database}
        if len(seen) > 1:
            raise_compiler_error(str(seen))

        for information_schema_name, schema in self.search():
            path = {
                'database': information_schema_name.database,
                'schema': schema
            }
            new.add(information_schema_name.incorporate(
                path=path,
                quote_policy={'database': False},
                include_policy={'database': False},
            ))

        return new


class BaseAdapter(metaclass=AdapterMeta):
    """The BaseAdapter provides an abstract base class for adapters.

    Adapters must implement the following methods and macros. Some of the
    methods can be safely overridden as a noop, where it makes sense
    (transactions on databases that don't support them, for instance). Those
    methods are marked with a (passable) in their docstrings. Check docstrings
    for type information, etc.

    To implement a macro, implement "${adapter_type}__${macro_name}". in the
    adapter's internal project.

    Methods:
        - exception_handler
        - date_function
        - list_schemas
        - drop_relation
        - truncate_relation
        - rename_relation
        - get_columns_in_relation
        - expand_column_types
        - list_relations_without_caching
        - is_cancelable
        - create_schema
        - drop_schema
        - quote
        - convert_text_type
        - convert_number_type
        - convert_boolean_type
        - convert_datetime_type
        - convert_date_type
        - convert_time_type

    Macros:
        - get_catalog
    """
    Relation: Type[BaseRelation] = BaseRelation
    Column: Type[BaseColumn] = BaseColumn
    ConnectionManager: Type[BaseConnectionManager]

    # A set of clobber config fields accepted by this adapter
    # for use in materializations
    AdapterSpecificConfigs: FrozenSet[str] = frozenset()

    def __init__(self, config):
        self.config = config
        self.cache = RelationsCache()
        self.connections = self.ConnectionManager(config)
        self._internal_manifest_lazy: Optional[Manifest] = None

    ###
    # Methods that pass through to the connection manager
    ###
    def acquire_connection(self, name=None) -> Connection:
        return self.connections.set_connection_name(name)

    def release_connection(self) -> None:
        self.connections.release()

    def cleanup_connections(self) -> None:
        self.connections.cleanup_all()

    def clear_transaction(self) -> None:
        self.connections.clear_transaction()

    def commit_if_has_connection(self) -> None:
        self.connections.commit_if_has_connection()

    def nice_connection_name(self) -> str:
        conn = self.connections.get_if_exists()
        if conn is None or conn.name is None:
            return '<None>'
        return conn.name

    @contextmanager
    def connection_named(
        self, name: str, node: Optional[CompileResultNode] = None
    ) -> Iterator[None]:
        try:
            self.connections.query_header.set(name, node)
            self.acquire_connection(name)
            yield
        finally:
            self.release_connection()
            self.connections.query_header.reset()

    @contextmanager
    def connection_for(
        self, node: CompileResultNode
    ) -> Iterator[None]:
        with self.connection_named(node.unique_id, node):
            yield

    @available.parse(lambda *a, **k: ('', empty_table()))
    def execute(
        self, sql: str, auto_begin: bool = False, fetch: bool = False
    ) -> Tuple[str, agate.Table]:
        """Execute the given SQL. This is a thin wrapper around
        ConnectionManager.execute.

        :param str sql: The sql to execute.
        :param bool auto_begin: If set, and dbt is not currently inside a
            transaction, automatically begin one.
        :param bool fetch: If set, fetch results.
        :return: A tuple of the status and the results (empty if fetch=False).
        :rtype: Tuple[str, agate.Table]
        """
        return self.connections.execute(
            sql=sql,
            auto_begin=auto_begin,
            fetch=fetch
        )

    ###
    # Methods that should never be overridden
    ###
    @classmethod
    def type(cls) -> str:
        """Get the type of this adapter. Types must be class-unique and
        consistent.

        :return: The type name
        :rtype: str
        """
        return cls.ConnectionManager.TYPE

    @property
    def _internal_manifest(self) -> Manifest:
        if self._internal_manifest_lazy is None:
            return self.load_internal_manifest()
        return self._internal_manifest_lazy

    def check_internal_manifest(self) -> Optional[Manifest]:
        """Return the internal manifest (used for executing macros) if it's
        been initialized, otherwise return None.
        """
        return self._internal_manifest_lazy

    def load_internal_manifest(self) -> Manifest:
        if self._internal_manifest_lazy is None:
            # avoid a circular import
            from dbt.parser.manifest import load_internal_manifest
            manifest = load_internal_manifest(self.config)
            self._internal_manifest_lazy = manifest
        return self._internal_manifest_lazy

    ###
    # Caching methods
    ###
    def _schema_is_cached(self, database: str, schema: str) -> bool:
        """Check if the schema is cached, and by default logs if it is not."""

        if dbt.flags.USE_CACHE is False:
            return False
        elif (database, schema) not in self.cache:
            logger.debug(
                'On "{}": cache miss for schema "{}.{}", this is inefficient'
                .format(self.nice_connection_name(), database, schema)
            )
            return False
        else:
            return True

    def _get_cache_schemas(
        self, manifest: Manifest, exec_only: bool = False
    ) -> SchemaSearchMap:
        """Get a mapping of each node's "information_schema" relations to a
        set of all schemas expected in that information_schema.

        There may be keys that are technically duplicates on the database side,
        for example all of '"foo", 'foo', '"FOO"' and 'FOO' could coexist as
        databases, and values could overlap as appropriate. All values are
        lowercase strings.
        """
        info_schema_name_map = SchemaSearchMap()
        for node in manifest.nodes.values():
            if exec_only and node.resource_type not in NodeType.executable():
                continue
            relation = self.Relation.create_from(self.config, node)
            info_schema_name_map.add(relation)
        # result is a map whose keys are information_schema Relations without
        # identifiers that have appropriate database prefixes, and whose values
        # are sets of lowercase schema names that are valid members of those
        # databases
        return info_schema_name_map

    def _relations_cache_for_schemas(self, manifest: Manifest) -> None:
        """Populate the relations cache for the given schemas. Returns an
        iterable of the schemas populated, as strings.
        """
        if not dbt.flags.USE_CACHE:
            return

        info_schema_name_map = self._get_cache_schemas(manifest,
                                                       exec_only=True)
        for db, schema in info_schema_name_map.search():
            for relation in self.list_relations_without_caching(db, schema):
                self.cache.add(relation)

        # it's possible that there were no relations in some schemas. We want
        # to insert the schemas we query into the cache's `.schemas` attribute
        # so we can check it later
        self.cache.update_schemas(info_schema_name_map.schemas_searched())

    def set_relations_cache(
        self, manifest: Manifest, clear: bool = False
    ) -> None:
        """Run a query that gets a populated cache of the relations in the
        database and set the cache on this adapter.
        """
        if not dbt.flags.USE_CACHE:
            return

        with self.cache.lock:
            if clear:
                self.cache.clear()
            self._relations_cache_for_schemas(manifest)

    @available
    def cache_added(self, relation: Optional[BaseRelation]) -> str:
        """Cache a new relation in dbt. It will show up in `list relations`."""
        if relation is None:
            name = self.nice_connection_name()
            raise_compiler_error(
                'Attempted to cache a null relation for {}'.format(name)
            )
        if dbt.flags.USE_CACHE:
            self.cache.add(relation)
        # so jinja doesn't render things
        return ''

    @available
    def cache_dropped(self, relation: Optional[BaseRelation]) -> str:
        """Drop a relation in dbt. It will no longer show up in
        `list relations`, and any bound views will be dropped from the cache
        """
        if relation is None:
            name = self.nice_connection_name()
            raise_compiler_error(
                'Attempted to drop a null relation for {}'.format(name)
            )
        if dbt.flags.USE_CACHE:
            self.cache.drop(relation)
        return ''

    @available
    def cache_renamed(
        self,
        from_relation: Optional[BaseRelation],
        to_relation: Optional[BaseRelation],
    ) -> str:
        """Rename a relation in dbt. It will show up with a new name in
        `list_relations`, but bound views will remain bound.
        """
        if from_relation is None or to_relation is None:
            name = self.nice_connection_name()
            src_name = _relation_name(from_relation)
            dst_name = _relation_name(to_relation)
            raise_compiler_error(
                'Attempted to rename {} to {} for {}'
                .format(src_name, dst_name, name)
            )

        if dbt.flags.USE_CACHE:
            self.cache.rename(from_relation, to_relation)
        return ''

    ###
    # Abstract methods for database-specific values, attributes, and types
    ###
    @abc.abstractclassmethod
    def date_function(cls) -> str:
        """Get the date function used by this adapter's database."""
        raise NotImplementedException(
            '`date_function` is not implemented for this adapter!')

    @abc.abstractclassmethod
    def is_cancelable(cls) -> bool:
        raise NotImplementedException(
            '`is_cancelable` is not implemented for this adapter!'
        )

    ###
    # Abstract methods about schemas
    ###
    @abc.abstractmethod
    def list_schemas(self, database: str) -> List[str]:
        """Get a list of existing schemas in database"""
        raise NotImplementedException(
            '`list_schemas` is not implemented for this adapter!'
        )

    @available.parse(lambda *a, **k: False)
    def check_schema_exists(self, database: str, schema: str) -> bool:
        """Check if a schema exists.

        The default implementation of this is potentially unnecessarily slow,
        and adapters should implement it if there is an optimized path (and
        there probably is)
        """
        search = (
            s.lower() for s in
            self.list_schemas(database=database)
        )
        return schema.lower() in search

    ###
    # Abstract methods about relations
    ###
    @abc.abstractmethod
    @available.parse_none
    def drop_relation(self, relation: BaseRelation) -> None:
        """Drop the given relation.

        *Implementors must call self.cache.drop() to preserve cache state!*
        """
        raise NotImplementedException(
            '`drop_relation` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    @available.parse_none
    def truncate_relation(self, relation: BaseRelation) -> None:
        """Truncate the given relation."""
        raise NotImplementedException(
            '`truncate_relation` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    @available.parse_none
    def rename_relation(
        self, from_relation: BaseRelation, to_relation: BaseRelation
    ) -> None:
        """Rename the relation from from_relation to to_relation.

        Implementors must call self.cache.rename() to preserve cache state.
        """
        raise NotImplementedException(
            '`rename_relation` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    @available.parse_list
    def get_columns_in_relation(
        self, relation: BaseRelation
    ) -> List[BaseColumn]:
        """Get a list of the columns in the given Relation."""
        raise NotImplementedException(
            '`get_columns_in_relation` is not implemented for this adapter!'
        )

    @available.deprecated('get_columns_in_relation', lambda *a, **k: [])
    def get_columns_in_table(
        self, schema: str, identifier: str
    ) -> List[BaseColumn]:
        """DEPRECATED: Get a list of the columns in the given table."""
        relation = self.Relation.create(
            database=self.config.credentials.database,
            schema=schema,
            identifier=identifier,
            quote_policy=self.config.quoting
        )
        return self.get_columns_in_relation(relation)

    @abc.abstractmethod
    def expand_column_types(
        self, goal: BaseRelation, current: BaseRelation
    ) -> None:
        """Expand the current table's types to match the goal table. (passable)

        :param self.Relation goal: A relation that currently exists in the
            database with columns of the desired types.
        :param self.Relation current: A relation that currently exists in the
            database with columns of unspecified types.
        """
        raise NotImplementedException(
            '`expand_target_column_types` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    def list_relations_without_caching(
        self, information_schema: BaseRelation, schema: str
    ) -> List[BaseRelation]:
        """List relations in the given schema, bypassing the cache.

        This is used as the underlying behavior to fill the cache.

        :param Relation information_schema: The information schema to list
            relations from.
        :param str schema: The name of the schema to list relations from.
        :return: The relations in schema
        :rtype: List[self.Relation]
        """
        raise NotImplementedException(
            '`list_relations_without_caching` is not implemented for this '
            'adapter!'
        )

    ###
    # Provided methods about relations
    ###
    @available.parse_list
    def get_missing_columns(
        self, from_relation: BaseRelation, to_relation: BaseRelation
    ) -> List[BaseColumn]:
        """Returns a list of Columns in from_relation that are missing from
        to_relation.
        """
        if not isinstance(from_relation, self.Relation):
            invalid_type_error(
                method_name='get_missing_columns',
                arg_name='from_relation',
                got_value=from_relation,
                expected_type=self.Relation)

        if not isinstance(to_relation, self.Relation):
            invalid_type_error(
                method_name='get_missing_columns',
                arg_name='to_relation',
                got_value=to_relation,
                expected_type=self.Relation)

        from_columns = {
            col.name: col for col in
            self.get_columns_in_relation(from_relation)
        }

        to_columns = {
            col.name: col for col in
            self.get_columns_in_relation(to_relation)
        }

        missing_columns = set(from_columns.keys()) - set(to_columns.keys())

        return [
            col for (col_name, col) in from_columns.items()
            if col_name in missing_columns
        ]

    @available.parse_none
    def valid_snapshot_target(self, relation: BaseRelation) -> None:
        """Ensure that the target relation is valid, by making sure it has the
        expected columns.

        :param Relation relation: The relation to check
        :raises CompilationException: If the columns are
            incorrect.
        """
        if not isinstance(relation, self.Relation):
            invalid_type_error(
                method_name='valid_snapshot_target',
                arg_name='relation',
                got_value=relation,
                expected_type=self.Relation)

        columns = self.get_columns_in_relation(relation)
        names = set(c.name.lower() for c in columns)
        expanded_keys = ('scd_id', 'valid_from', 'valid_to')
        extra = []
        missing = []
        for legacy in expanded_keys:
            desired = 'dbt_' + legacy
            if desired not in names:
                missing.append(desired)
                if legacy in names:
                    extra.append(legacy)

        if missing:
            if extra:
                msg = (
                    'Snapshot target has ("{}") but not ("{}") - is it an '
                    'unmigrated previous version archive?'
                    .format('", "'.join(extra), '", "'.join(missing))
                )
            else:
                msg = (
                    'Snapshot target is not a snapshot table (missing "{}")'
                    .format('", "'.join(missing))
                )
            raise_compiler_error(msg)

    @available.parse_none
    def expand_target_column_types(
        self, from_relation: BaseRelation, to_relation: BaseRelation
    ) -> None:
        if not isinstance(from_relation, self.Relation):
            invalid_type_error(
                method_name='expand_target_column_types',
                arg_name='from_relation',
                got_value=from_relation,
                expected_type=self.Relation)

        if not isinstance(to_relation, self.Relation):
            invalid_type_error(
                method_name='expand_target_column_types',
                arg_name='to_relation',
                got_value=to_relation,
                expected_type=self.Relation)

        self.expand_column_types(from_relation, to_relation)

    def list_relations(self, database: str, schema: str) -> List[BaseRelation]:
        if self._schema_is_cached(database, schema):
            return self.cache.get_relations(database, schema)

        information_schema = self.Relation.create(
            database=database,
            schema=schema,
            identifier='',
            quote_policy=self.config.quoting
        ).information_schema()

        # we can't build the relations cache because we don't have a
        # manifest so we can't run any operations.
        relations = self.list_relations_without_caching(
            information_schema, schema
        )

        logger.debug('with database={}, schema={}, relations={}'
                     .format(database, schema, relations))
        return relations

    def _make_match_kwargs(
        self, database: str, schema: str, identifier: str
    ) -> Dict[str, str]:
        quoting = self.config.quoting
        if identifier is not None and quoting['identifier'] is False:
            identifier = identifier.lower()

        if schema is not None and quoting['schema'] is False:
            schema = schema.lower()

        if database is not None and quoting['database'] is False:
            database = database.lower()

        return filter_null_values({
            'database': database,
            'identifier': identifier,
            'schema': schema,
        })

    def _make_match(
        self,
        relations_list: List[BaseRelation],
        database: str,
        schema: str,
        identifier: str,
    ) -> List[BaseRelation]:

        matches = []

        search = self._make_match_kwargs(database, schema, identifier)

        for relation in relations_list:
            if relation.matches(**search):
                matches.append(relation)

        return matches

    @available.parse_none
    def get_relation(
        self, database: str, schema: str, identifier: str
    ) -> Optional[BaseRelation]:
        relations_list = self.list_relations(database, schema)

        matches = self._make_match(relations_list, database, schema,
                                   identifier)

        if len(matches) > 1:
            kwargs = {
                'identifier': identifier,
                'schema': schema,
                'database': database,
            }
            get_relation_returned_multiple_results(
                kwargs, matches
            )

        elif matches:
            return matches[0]

        return None

    @available.deprecated('get_relation', lambda *a, **k: False)
    def already_exists(self, schema: str, name: str) -> bool:
        """DEPRECATED: Return if a model already exists in the database"""
        database = self.config.credentials.database
        relation = self.get_relation(database, schema, name)
        return relation is not None

    ###
    # ODBC FUNCTIONS -- these should not need to change for every adapter,
    #                   although some adapters may override them
    ###
    @abc.abstractmethod
    @available.parse_none
    def create_schema(self, database: str, schema: str):
        """Create the given schema if it does not exist."""
        raise NotImplementedException(
            '`create_schema` is not implemented for this adapter!'
        )

    @abc.abstractmethod
    @available.parse_none
    def drop_schema(self, database: str, schema: str):
        """Drop the given schema (and everything in it) if it exists."""
        raise NotImplementedException(
            '`drop_schema` is not implemented for this adapter!'
        )

    @available
    @abc.abstractclassmethod
    def quote(cls, identifier: str) -> str:
        """Quote the given identifier, as appropriate for the database."""
        raise NotImplementedException(
            '`quote` is not implemented for this adapter!'
        )

    @available
    def quote_as_configured(self, identifier: str, quote_key: str) -> str:
        """Quote or do not quote the given identifer as configured in the
        project config for the quote key.

        The quote key should be one of 'database' (on bigquery, 'profile'),
        'identifier', or 'schema', or it will be treated as if you set `True`.
        """
        try:
            key = ComponentName(quote_key)
        except ValueError:
            return identifier

        default = self.Relation.get_default_quote_policy().get_part(key)
        if self.config.quoting.get(key, default):
            return self.quote(identifier)
        else:
            return identifier

    @available
    def quote_seed_column(
        self, column: str, quote_config: Optional[bool]
    ) -> str:
        # this is the default for now
        quote_columns: bool = False
        if isinstance(quote_config, bool):
            quote_columns = quote_config
        elif quote_config is None:
            deprecations.warn('column-quoting-unset')
        else:
            raise_compiler_error(
                f'The seed configuration value of "quote_columns" has an '
                f'invalid type {type(quote_config)}'
            )

        if quote_columns:
            return self.quote(column)
        else:
            return column

    ###
    # Conversions: These must be implemented by concrete implementations, for
    # converting agate types into their sql equivalents.
    ###
    @abc.abstractclassmethod
    def convert_text_type(
        cls, agate_table: agate.Table, col_idx: int
    ) -> str:
        """Return the type in the database that best maps to the agate.Text
        type for the given agate table and column index.

        :param agate_table: The table
        :param col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        """
        raise NotImplementedException(
            '`convert_text_type` is not implemented for this adapter!')

    @abc.abstractclassmethod
    def convert_number_type(
        cls, agate_table: agate.Table, col_idx: int
    ) -> str:
        """Return the type in the database that best maps to the agate.Number
        type for the given agate table and column index.

        :param agate_table: The table
        :param col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        """
        raise NotImplementedException(
            '`convert_number_type` is not implemented for this adapter!')

    @abc.abstractclassmethod
    def convert_boolean_type(
        cls, agate_table: agate.Table, col_idx: int
    ) -> str:
        """Return the type in the database that best maps to the agate.Boolean
        type for the given agate table and column index.

        :param agate_table: The table
        :param col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        """
        raise NotImplementedException(
            '`convert_boolean_type` is not implemented for this adapter!')

    @abc.abstractclassmethod
    def convert_datetime_type(
        cls, agate_table: agate.Table, col_idx: int
    ) -> str:
        """Return the type in the database that best maps to the agate.DateTime
        type for the given agate table and column index.

        :param agate_table: The table
        :param col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        """
        raise NotImplementedException(
            '`convert_datetime_type` is not implemented for this adapter!')

    @abc.abstractclassmethod
    def convert_date_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        """Return the type in the database that best maps to the agate.Date
        type for the given agate table and column index.

        :param agate_table: The table
        :param col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        """
        raise NotImplementedException(
            '`convert_date_type` is not implemented for this adapter!')

    @abc.abstractclassmethod
    def convert_time_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        """Return the type in the database that best maps to the
        agate.TimeDelta type for the given agate table and column index.

        :param agate_table: The table
        :param col_idx: The index into the agate table for the column.
        :return: The name of the type in the database
        """
        raise NotImplementedException(
            '`convert_time_type` is not implemented for this adapter!')

    @available
    @classmethod
    def convert_type(cls, agate_table, col_idx):
        return cls.convert_agate_type(agate_table, col_idx)

    @classmethod
    def convert_agate_type(cls, agate_table, col_idx):
        agate_type = agate_table.column_types[col_idx]
        conversions = [
            (agate.Text, cls.convert_text_type),
            (agate.Number, cls.convert_number_type),
            (agate.Boolean, cls.convert_boolean_type),
            (agate.DateTime, cls.convert_datetime_type),
            (agate.Date, cls.convert_date_type),
            (agate.TimeDelta, cls.convert_time_type),
        ]
        for agate_cls, func in conversions:
            if isinstance(agate_type, agate_cls):
                return func(agate_table, col_idx)

    ###
    # Operations involving the manifest
    ###
    def execute_macro(
        self,
        macro_name: str,
        manifest: Optional[Manifest] = None,
        project: Optional[str] = None,
        context_override: Optional[Dict[str, Any]] = None,
        kwargs: Dict[str, Any] = None,
        release: bool = False,
    ) -> agate.Table:
        """Look macro_name up in the manifest and execute its results.

        :param macro_name: The name of the macro to execute.
        :param manifest: The manifest to use for generating the base macro
            execution context. If none is provided, use the internal manifest.
        :param project: The name of the project to search in, or None for the
            first match.
        :param context_override: An optional dict to update() the macro
            execution context.
        :param kwargs: An optional dict of keyword args used to pass to the
            macro.
        :param release: If True, release the connection after executing.
        """
        if kwargs is None:
            kwargs = {}
        if context_override is None:
            context_override = {}

        if manifest is None:
            manifest = self._internal_manifest

        macro = manifest.find_macro_by_name(
            macro_name, self.config.project_name, project
        )
        if macro is None:
            if project is None:
                package_name = 'any package'
            else:
                package_name = 'the "{}" package'.format(project)

            raise RuntimeException(
                'dbt could not find a macro with the name "{}" in {}'
                .format(macro_name, package_name)
            )
        # This causes a reference cycle, as dbt.context.runtime.generate()
        # ends up calling get_adapter, so the import has to be here.
        import dbt.context.operation
        macro_context = dbt.context.operation.generate(
            model=macro,
            runtime_config=self.config,
            manifest=manifest,
            package_name=project
        )
        macro_context.update(context_override)

        macro_function = macro.generator(macro_context)

        try:
            result = macro_function(**kwargs)
        finally:
            if release:
                self.release_connection()
        return result

    @classmethod
    def _catalog_filter_table(
        cls, table: agate.Table, manifest: Manifest
    ) -> agate.Table:
        """Filter the table as appropriate for catalog entries. Subclasses can
        override this to change filtering rules on a per-adapter basis.
        """
        return table.where(_catalog_filter_schemas(manifest))

    def _get_catalog_information_schemas(
        self, manifest: Manifest
    ) -> List[InformationSchema]:
        return list(self._get_cache_schemas(manifest).keys())

    def get_catalog(self, manifest: Manifest) -> agate.Table:
        """Get the catalog for this manifest by running the get catalog macro.
        Returns an agate.Table of catalog information.
        """
        information_schemas = self._get_catalog_information_schemas(manifest)
        # make it a list so macros can index into it.
        kwargs = {'information_schemas': information_schemas}
        table = self.execute_macro(
            GET_CATALOG_MACRO_NAME,
            kwargs=kwargs,
            release=True,
            # pass in the full manifest so we get any local project overrides
            manifest=manifest,
        )

        results = self._catalog_filter_table(table, manifest)
        return results

    def cancel_open_connections(self):
        """Cancel all open connections."""
        return self.connections.cancel_open()

    def calculate_freshness(
        self,
        source: BaseRelation,
        loaded_at_field: str,
        filter: Optional[str],
        manifest: Optional[Manifest] = None
    ) -> Dict[str, Any]:
        """Calculate the freshness of sources in dbt, and return it"""
        kwargs: Dict[str, Any] = {
            'source': source,
            'loaded_at_field': loaded_at_field,
            'filter': filter,
        }

        # run the macro
        table = self.execute_macro(
            FRESHNESS_MACRO_NAME,
            kwargs=kwargs,
            release=True,
            manifest=manifest
        )
        # now we have a 1-row table of the maximum `loaded_at_field` value and
        # the current time according to the db.
        if len(table) != 1 or len(table[0]) != 2:
            raise_compiler_error(
                'Got an invalid result from "{}" macro: {}'.format(
                    FRESHNESS_MACRO_NAME, [tuple(r) for r in table]
                )
            )
        if table[0][0] is None:
            # no records in the table, so really the max_loaded_at was
            # infinitely long ago. Just call it 0:00 January 1 year UTC
            max_loaded_at = datetime(1, 1, 1, 0, 0, 0, tzinfo=pytz.UTC)
        else:
            max_loaded_at = _utc(table[0][0], source, loaded_at_field)

        snapshotted_at = _utc(table[0][1], source, loaded_at_field)
        age = (snapshotted_at - max_loaded_at).total_seconds()
        return {
            'max_loaded_at': max_loaded_at,
            'snapshotted_at': snapshotted_at,
            'age': age,
        }

    def pre_model_hook(self, config: Mapping[str, Any]) -> Any:
        """A hook for running some operation before the model materialization
        runs. The hook can assume it has a connection available.

        The only parameter is a configuration dictionary (the same one
        available in the materialization context). It should be considered
        read-only.

        The pre-model hook may return anything as a context, which will be
        passed to the post-model hook.
        """
        pass

    def post_model_hook(self, config: Mapping[str, Any], context: Any) -> None:
        """A hook for running some operation after the model materialization
        runs. The hook can assume it has a connection available.

        The first parameter is a configuration dictionary (the same one
        available in the materialization context). It should be considered
        read-only.

        The second parameter is the value returned by pre_mdoel_hook.
        """
        pass
