from uuid import uuid4
import agate
import re
import boto3.session
from botocore.exceptions import ClientError
from typing import Optional

from dbt.adapters.base import available
from dbt.adapters.sql import SQLAdapter
from dbt.adapters.athena import AthenaConnectionManager
from dbt.adapters.athena.relation import AthenaRelation
from dbt.events import AdapterLogger
logger = AdapterLogger("Athena")

class AthenaAdapter(SQLAdapter):
    ConnectionManager = AthenaConnectionManager
    Relation = AthenaRelation

    @classmethod
    def date_function(cls) -> str:
        return "now()"

    @classmethod
    def convert_text_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "string"

    @classmethod
    def convert_number_type(
        cls, agate_table: agate.Table, col_idx: int
    ) -> str:
        decimals = agate_table.aggregate(agate.MaxPrecision(col_idx))
        return "double" if decimals else "integer"

    @classmethod
    def convert_datetime_type(
            cls, agate_table: agate.Table, col_idx: int
    ) -> str:
        return "timestamp"

    @available
    def s3_table_prefix(self) -> str:
        """
        Returns the root location for storing tables in S3.

        This is `s3_data_dir`, if set, and `s3_staging_dir/tables/` if not.

        We generate a value here even if `s3_data_dir` is not set,
        since creating a seed table requires a non-default location.
        """
        conn = self.connections.get_thread_connection()
        creds = conn.credentials
        if creds.s3_data_dir is not None:
            return creds.s3_data_dir
        else:
            return f"{creds.s3_staging_dir}tables/"

    @available
    def s3_uuid_table_location(self) -> str:
        """
        Returns a random location for storing a table, using a UUID as
        the final directory part
        """
        return f"{self.s3_table_prefix()}{str(uuid4())}/"


    @available
    def temp_table_suffix(self, initial="__dbt_tmp", length=8):
        return f"{initial}_{str(uuid4())[:length]}"


    @available
    def s3_schema_table_location(self, schema_name: str, table_name: str) -> str:
        """
        Returns a fixed location for storing a table determined by the
        (athena) schema and table name
        """
        return f"{self.s3_table_prefix()}{schema_name}/{table_name}/"

    @available
    def s3_table_location(self, schema_name: str, table_name: str) -> str:
        """
        Returns either a UUID or database/table prefix for storing a table,
        depending on the value of s3_table
        """
        conn = self.connections.get_thread_connection()
        creds = conn.credentials
        if creds.s3_data_naming == "schema_table":
            return self.s3_schema_table_location(schema_name, table_name)
        elif creds.s3_data_naming == "uuid":
            return self.s3_uuid_table_location()
        else:
            raise ValueError(f"Unknown value for s3_data_naming: {creds.s3_data_naming}")

    @available
    def has_s3_data_dir(self) -> bool:
        """
        Returns true if the user has specified `s3_data_dir`, and
        we should set `external_location
        """
        conn = self.connections.get_thread_connection()
        creds = conn.credentials
        return creds.s3_data_dir is not None


    @available
    def clean_up_partitions(
        self, database_name: str, table_name: str, where_condition: str
    ):
        # Look up Glue partitions & clean up
        conn = self.connections.get_thread_connection()
        creds = conn.credentials
        session = boto3.session.Session(region_name=creds.region_name, profile_name=creds.aws_profile_name)

        glue_client = session.client('glue')
        s3_resource = session.resource('s3')
        paginator = glue_client.get_paginator("get_partitions")
        partition_pages = paginator.paginate(
            # CatalogId='123456789012', # Need to make this configurable if it is different from default AWS Account ID
            DatabaseName=database_name,
            TableName=table_name,
            Expression=where_condition,
            ExcludeColumnSchema=True,
        )
        partitions = []
        for page in partition_pages:
            partitions.extend(page["Partitions"])
        p = re.compile('s3://([^/]*)/(.*)')
        for partition in partitions:
            logger.debug("Deleting objects for partition '{}' at '{}'", partition["Values"], partition["StorageDescriptor"]["Location"])
            m = p.match(partition["StorageDescriptor"]["Location"])
            if m is not None:
                bucket_name = m.group(1)
                prefix = m.group(2)
                s3_bucket = s3_resource.Bucket(bucket_name)
                s3_bucket.objects.filter(Prefix=prefix).delete()

    @available
    def clean_up_table(
        self, database_name: str, table_name: str
    ):
        # Look up Glue partitions & clean up
        conn = self.connections.get_thread_connection()
        creds = conn.credentials
        session = boto3.session.Session(region_name=creds.region_name, profile_name=creds.aws_profile_name)

        glue_client = session.client('glue')
        try:
            table = glue_client.get_table(
                DatabaseName=database_name,
                Name=table_name
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'EntityNotFoundException':
                logger.debug("Table '{}' does not exists - Ignoring", table_name)
                return

        if table is not None:
            logger.debug("Deleting table data from'{}'", table["Table"]["StorageDescriptor"]["Location"])
            p = re.compile('s3://([^/]*)/(.*)')
            m = p.match(table["Table"]["StorageDescriptor"]["Location"])
            if m is not None:
                bucket_name = m.group(1)
                prefix = m.group(2)
                s3_resource = session.resource('s3')
                s3_bucket = s3_resource.Bucket(bucket_name)
                s3_bucket.objects.filter(Prefix=prefix).delete()

    @available
    def quote_seed_column(
        self, column: str, quote_config: Optional[bool]
    ) -> str:
        return super().quote_seed_column(column, False)
