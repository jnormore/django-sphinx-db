# coding: utf-8

from django.db.models.sql import compiler
from django.db.models.sql.query import get_order_dir, ORDER_DIR

from django.db.models.sql.where import WhereNode, ExtraWhere, AND
from django.db.models.sql.where import EmptyShortCircuit, EmptyResultSet
from django.db.models.sql.expressions import SQLEvaluator
import re


class SphinxExtraWhere(ExtraWhere):

    def as_sql(self, qn=None, connection=None):
        sqls = ["%s" % sql for sql in self.sqls]
        return " AND ".join(sqls), tuple(self.params or ())


class SphinxWhereNode(WhereNode):
    def sql_for_columns(self, data, qn, connection):
        table_alias, name, db_type = data
        return connection.ops.field_cast_sql(db_type) % name

    def make_atom(self, child, qn, connection):
        """
        Transform search, the keyword should not be quoted.
        """
        lvalue, lookup_type, value_annot, params_or_value = child
        sql, params = super(SphinxWhereNode, self).make_atom(child, qn, connection)
        if lookup_type == 'search':
            if hasattr(lvalue, 'process'):
                try:
                    lvalue, params = lvalue.process(lookup_type, params_or_value, connection)
                except EmptyShortCircuit:
                    raise EmptyResultSet
            if isinstance(lvalue, tuple):
                # A direct database column lookup.
                field_sql = self.sql_for_columns(lvalue, qn, connection)
            else:
                # A smart object with an as_sql() method.
                field_sql = lvalue.as_sql(qn, connection)
            # TODO: There are a couple problems here.
            # 1. The user _might_ want to search only a specific field.
            # 2. However, since Django requires a field name to use the __search operator
            #    There is no way to do a search in _all_ fields.
            # 3. Because, using multiple __search operators is not supported.
            # So, we need to merge multiped __search operators into a single MATCH(), we
            # can't do that here, we have to do that one level up...
            # Ignore the field name, search all fields:
            params = ('@* %s' % params[0], )
            # _OR_ respect the field name, and search on it:
            #params = ('@%s %s' % (field_sql, params[0]), )
        return sql, params


class SphinxQLCompiler(compiler.SQLCompiler):
    def get_columns(self, *args, **kwargs):
        columns = super(SphinxQLCompiler, self).get_columns(*args, **kwargs)
        db_table = self.query.model._meta.db_table
        for i, column in enumerate(columns):
            if column.startswith(db_table + '.'):
                column = column.partition('.')[2]
            # TODO: remove if this code is not used
            # fix not accepted expression (weight()) AS w
            #columns[i] = re.sub(r"^\((.*)\) AS ([\w\d\_]+)$", '\\1 AS \\2',
            #                    column)
            columns[i] = column
        return columns

    def quote_name_unless_alias(self, name):
        # TODO: remove this when no longer needed.
        # This is to remove the `` backticks from identifiers.
        # http://sphinxsearch.com/bugs/view.php?id=1150
        # while bug is closed, () and `` together still cause syntax error
        return name

    def get_ordering(self):
        """ Remove index name (model.Meta.db_table) from ORDER_BY clause."""
        result, group_by = super(SphinxQLCompiler, self).get_ordering()

        # excluding from ordering_group_by items added from "extra_select"
        exclude = {g[0] for g in self.query.extra_select.values()}
        group_by = [g for g in group_by if g[0] not in exclude]

        # processing result ('idx.field1', 'idx.field2')
        func = lambda name: name.split('.', 1)[-1]
        result = map(func, result)

        # processing group_by tuples: (('idx.field1', []), ('idx.field2', []))
        group_by = map(lambda t: (func(t[0]),) + t[1:], group_by)

        # TODO: process self.query.ordering_aliases
        # self.query.ordering_aliases is also set by parent get_ordering()
        # method, and it also may contain db_table name.
        return result, group_by

    def get_grouping(self, ordering_group_by):
        result, params = super(SphinxQLCompiler, self).get_grouping(
            ordering_group_by)

        # removing parentheses from group by fields
        for i in range(len(result)):
            g = result[i]
            if g[0] == '(' and g[-1] == ')':
                result[i] = g[1:-1]

        # excluding from ordering_group_by items added from "extra_select"
        exclude = {g[0] for g in self.query.extra_select.values()}
        result = [g for g in result if g not in exclude]
        return result, params

    def as_sql(self, with_limits=True, with_col_aliases=False):
        """ Patching final SQL query."""
        match = getattr(self.query, 'match', None)
        if match:
            match = "MATCH('%s')" % ' '.join(expr for expr in match)
            self.query.where.add(SphinxExtraWhere([match], []), AND)

        sql, args = super(SphinxQLCompiler, self).as_sql(with_limits,
                                                         with_col_aliases)
        # removing unsupported OFFSET clause
        # replacing it with LIMIT <offset>, <limit>
        sql = re.sub(r'LIMIT ([\d]+) OFFSET ([\d]+)$', 'LIMIT \\2, \\1', sql)

        # patching GROUP BY clause
        group_by_limit = getattr(self.query, 'group_by_limit', '')
        group_by_ordering = self.get_group_ordering()
        if group_by_limit:
            # add GROUP <N> BY expression
            group_by = 'GROUP %s BY \\1' % group_by_limit
        else:
            group_by = 'GROUP BY \\1'
        if group_by_ordering:
            # add WITHIN GROUP ORDER BY expression
            group_by += group_by_ordering
        sql = re.sub(r'GROUP BY (([\w\d_]+)(, [\w\d_]+)*)', group_by, sql)

        # adding sphinx OPTION clause
        # TODO: syntax check for option values is not performed
        options = getattr(self.query, 'options', None)
        if options:
            sql += ' OPTION %s' % ', '.join(
                ["%s=%s" % i for i in options.items()]) or ''

        # percents, added by raw formatting queries, escaped as %%
        sql = re.sub(r'(%[^s])', '%%\1', sql)
        return sql, args

    def get_group_ordering(self):
        group_order_by = getattr(self.query, 'group_order_by', ())
        asc, desc = ORDER_DIR['ASC']
        if not group_order_by:
            return ''
        result = []
        for order_by in group_order_by:
            col, order = get_order_dir(order_by, asc)
            result.append("%s %s" % (col, order))
        return " WITHIN GROUP ORDER BY " + ", ".join(result)

# Set SQLCompiler appropriately, so queries will use the correct compiler.
SQLCompiler = SphinxQLCompiler


class SQLInsertCompiler(compiler.SQLInsertCompiler, SphinxQLCompiler):
    pass


class SQLDeleteCompiler(compiler.SQLDeleteCompiler, SphinxQLCompiler):
    pass


class SQLUpdateCompiler(compiler.SQLUpdateCompiler, SphinxQLCompiler):
    def as_sql(self):
        qn = self.connection.ops.quote_name
        opts = self.query.model._meta
        result = ['REPLACE INTO %s' % qn(opts.db_table)]
        # This is a bit ugly, we have to scrape information from the where clause
        # and put it into the field/values list. Sphinx will not accept an UPDATE
        # statement that includes full text data, only INSERT/REPLACE INTO.
        lvalue, lookup_type, value_annot, params_or_value = self.query.where.children[0].children[0]
        (table_name, column_name, column_type), val = lvalue.process(lookup_type, params_or_value, self.connection)
        fields, values, params = [column_name], ['%s'], [val[0]]
        # Now build the rest of the fields into our query.
        for field, model, val in self.query.values:
            if hasattr(val, 'prepare_database_save'):
                val = val.prepare_database_save(field)
            else:
                val = field.get_db_prep_save(val, connection=self.connection)

            # Getting the placeholder for the field.
            if hasattr(field, 'get_placeholder'):
                placeholder = field.get_placeholder(val, self.connection)
            else:
                placeholder = '%s'

            if hasattr(val, 'evaluate'):
                val = SQLEvaluator(val, self.query, allow_joins=False)
            name = field.column
            if hasattr(val, 'as_sql'):
                sql, params = val.as_sql(qn, self.connection)
                values.append(sql)
                params.extend(params)
            elif val is not None:
                values.append(placeholder)
                params.append(val)
            else:
                values.append('NULL')
            fields.append(name)
        result.append('(%s)' % ', '.join(fields))
        result.append('VALUES (%s)' % ', '.join(values))
        return ' '.join(result), params


class SQLAggregateCompiler(compiler.SQLAggregateCompiler, SphinxQLCompiler):
    pass


class SQLDateCompiler(compiler.SQLDateCompiler, SphinxQLCompiler):
    pass
