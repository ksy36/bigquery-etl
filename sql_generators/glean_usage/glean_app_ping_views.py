"""
Generate app (as opposed to channel) specific views for Glean ping tables.

The generation logic sets fields that might not be present in stable tables
(but are present in others) to NULL. Fields are ordered so that UNIONs across
the stable tables are possible.
For views that have incomaptible schemas (e.g due to fields having mismatching
types), the view is only generated for the release channel.
"""
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from mozilla_schema_generator.glean_ping import GleanPing

from bigquery_etl.format_sql.formatter import reformat
from bigquery_etl.schema import Schema
from bigquery_etl.util.common import get_table_dir, write_sql
from sql_generators.glean_usage.common import GleanTable

VIEW_METADATA_TEMPLATE = """\
# Generated by bigquery_etl.glean_usage.GleanAppPingViews
---
friendly_name: App-specific view for Glean ping "{ping_name}"
description: |-
  This a view that UNIONs the stable ping tables
  across all channels of the Glean application "{app_name}"
  ({app_channels}).

  It is used by Looker.
"""

PATH = Path(os.path.dirname(__file__))


class GleanAppPingViews(GleanTable):
    """Represents generated Glean app ping view."""

    def __init__(self):
        """Initialize Glean ping view."""
        GleanTable.__init__(self)
        self.no_init = True
        self.per_app_id_enabled = False
        self.per_app_enabled = True

    def generate_per_app(self, project_id, app_info, output_dir=None):
        """
        Generate per-app ping views across channels.

        If schemas are incompatible, then use release channel only.
        """

        # get release channel info
        release_app = app_info[0]
        target_dataset = release_app["app_name"]

        # channels are all in the same repo, sending the same pings
        repo = next(
            (r for r in GleanPing.get_repos() if r["name"] == release_app["v1_name"])
        )

        # app name is the same as the bq_dataset_family for the release channel: do nothing
        if (
            repo["app_id"] == release_app["app_name"]
            or release_app["bq_dataset_family"] == release_app["app_name"]
        ):
            return

        env = Environment(loader=FileSystemLoader(PATH / "templates"))
        view_template = env.get_template("app_ping_view.view.sql")

        p = GleanPing(repo)
        # generate views for all available pings
        for ping_name in p.get_pings():
            view_name = ping_name.replace("-", "_")
            full_view_id = f"moz-fx-data-shared-prod.{target_dataset}.{view_name}"

            # generate a unioned schema that contains all fields of all ping tables across all channels
            unioned_schema = Schema.empty()

            # cache schemas to be reused when generating the select expression
            cached_schemas = {}

            # iterate through app_info to get all channels
            for channel in app_info:
                channel_dataset = channel["document_namespace"].replace("-", "_")
                schema = Schema.for_table(
                    "moz-fx-data-shared-prod",
                    channel_dataset,
                    view_name,
                    partitioned_by="submission_timestamp",
                )
                cached_schemas[channel_dataset] = schema

                try:
                    unioned_schema.merge(schema, add_missing_fields=True, ignore_incompatible_fields=True)
                except Exception as e:
                    # if schema incompatibilities are detected, then only generate for release channel
                    print(
                        f"Cannot UNION 'moz-fx-data-shared-prod.{channel_dataset}.{view_name}': {e}"
                    )

                    break

            # generate the SELECT expression used for UNIONing the stable tables;
            # fields that are not part of a table, but exist in others, are set to NULL
            queries = []
            for app in app_info:
                channel_dataset = app["document_namespace"].replace("-", "_")

                if channel_dataset not in cached_schemas or cached_schemas[channel_dataset].schema["fields"] == []:
                    # check for empty schemas (e.g. restricted ones) and skip for now
                    print(f"Cannot get schema for `{channel_dataset}.{view_name}`; Skipping")
                    continue

                # compare table schema with unioned schema to determine fields that need to be NULL
                select_expression = self._generate_select_expression(
                    unioned_schema.schema["fields"], cached_schemas[channel_dataset].schema["fields"]
                )

                queries.append(
                    dict(
                        select_expression=select_expression,
                        dataset=channel_dataset,
                        table=view_name,
                    )
                )

            if queries == []:
                # nothing to render
                continue

            # render view SQL
            render_kwargs = dict(
                project_id=project_id, target_view=full_view_id, queries=queries
            )
            rendered_view = reformat(view_template.render(**render_kwargs))

            # write generated SQL files to destination folders
            if output_dir:
                write_sql(
                    output_dir,
                    full_view_id,
                    "view.sql",
                    rendered_view,
                    skip_existing=True,
                )

                app_channels = [f"{channel['dataset']}.{view_name}" for channel in queries]

                write_sql(
                    output_dir,
                    full_view_id,
                    "metadata.yaml",
                    VIEW_METADATA_TEMPLATE.format(
                        ping_name=ping_name,
                        app_name=release_app["canonical_app_name"],
                        app_channels=", ".join(app_channels),
                    ),
                    skip_existing=True,
                )

                schema_dir = get_table_dir(output_dir, full_view_id)
                unioned_schema.to_yaml_file(schema_dir / "schema.yaml")

    def _generate_select_expression(
        self, unioned_schema_nodes, app_schema_nodes, path=[]
    ) -> str:
        """
        Generate the select expression based on the unioned schema and the app channel schema.

        Any fields that are missing in the app_schema are set to NULL.
        """
        select_expr = []
        unioned_schema_nodes = {n["name"]: n for n in unioned_schema_nodes}
        app_schema_nodes = {n["name"]: n for n in app_schema_nodes}

        # iterate through fields
        for node_name, node in unioned_schema_nodes.items():
            dtype = node["type"]

            if node_name in app_schema_nodes:
                # field exists in app schema

                if node == app_schema_nodes[node_name]:
                    # field (and all nested fields) are identical, so just query it
                    select_expr.append(f"{'.'.join(path + [node_name])}")
                else:
                    # fields, and/or nested fields are not identical
                    if dtype == "RECORD":
                        # for nested fields, recursively generate select expression

                        if node.get("mode", None) == "REPEATED":
                            # unnest repeated record
                            select_expr.append(f"""
                                ARRAY_AGG(
                                    (   SELECT
                                        STRUCT(
                                            {self._generate_select_expression(node['fields'], app_schema_nodes[node_name]['fields'], path + [node_name])}
                                        )
                                        FROM UNNEST({node_name}) AS {node_name}
                                    )
                                ) AS {node_name}
                            """)
                        else:
                            # select struct fields
                            select_expr.append(f"""
                                STRUCT(
                                    {self._generate_select_expression(node['fields'], app_schema_nodes[node_name]['fields'], path + [node_name])}
                                ) AS {node_name}
                            """)
                    else:
                        select_expr.append(f"NULL AS {node_name}")
            else:
                # field doesn't exist in app schema, set to NULL
                select_expr.append(f"NULL AS {node_name}")

        return ", ".join(select_expr)
