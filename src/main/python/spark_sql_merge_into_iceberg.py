#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
# vim: tabstop=2 shiftwidth=2 softtabstop=2 expandtab

import os
import sys
import traceback

from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue import DynamicFrame

from pyspark.context import SparkContext
from pyspark.conf import SparkConf
from pyspark.sql import DataFrame, Row
from pyspark.sql.window import Window
from pyspark.sql.functions import (
  col,
  desc,
  row_number,
  to_timestamp
)

args = getResolvedOptions(sys.argv, ['JOB_NAME',
  'catalog',
  'database_name',
  'table_name',
  'primary_key',
  'kafka_topic_name',
  'starting_offsets_of_kafka_topic',
  'kafka_connection_name',
  'iceberg_s3_path',
  'lock_table_name',
  'aws_region',
  'window_size'
])

CATALOG = args['catalog']

ICEBERG_S3_PATH = args['iceberg_s3_path']

DATABASE = args['database_name']
TABLE_NAME = args['table_name']
PRIMARY_KEY = args['primary_key']

DYNAMODB_LOCK_TABLE = args['lock_table_name']

KAFKA_TOPIC_NAME = args['kafka_topic_name']
KAFKA_CONNECTION_NAME = args['kafka_connection_name']

#XXX: starting_offsets_of_kafka_topic: ['latest', 'earliest']
STARTING_OFFSETS_OF_KAFKA_TOPIC = args.get('starting_offsets_of_kafka_topic', 'latest')

AWS_REGION = args['aws_region']
WINDOW_SIZE = args.get('window_size', '100 seconds')

def setSparkIcebergConf() -> SparkConf:
  conf_list = [
    (f"spark.sql.catalog.{CATALOG}", "org.apache.iceberg.spark.SparkCatalog"),
    (f"spark.sql.catalog.{CATALOG}.warehouse", ICEBERG_S3_PATH),
    (f"spark.sql.catalog.{CATALOG}.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog"),
    (f"spark.sql.catalog.{CATALOG}.io-impl", "org.apache.iceberg.aws.s3.S3FileIO"),
    (f"spark.sql.catalog.{CATALOG}.lock-impl", "org.apache.iceberg.aws.glue.DynamoLockManager"),
    (f"spark.sql.catalog.{CATALOG}.lock.table", DYNAMODB_LOCK_TABLE),
    ("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"),
    ("spark.sql.iceberg.handle-timestamp-without-timezone", "true")
  ]
  spark_conf = SparkConf().setAll(conf_list)
  return spark_conf

# Set the Spark + Glue context
conf = setSparkIcebergConf()
sc = SparkContext(conf=conf)
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

#XXX: For more infomation, see
# https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-etl-connect-kafka-home.html#aws-glue-programming-etl-connect-kafka
kafka_options = {
  "connectionName": KAFKA_CONNECTION_NAME,
  "topicName": KAFKA_TOPIC_NAME,
  "startingOffsets": STARTING_OFFSETS_OF_KAFKA_TOPIC,
  "inferSchema": "true",
  "classification": "json",

  #XXX: the properties below are required for IAM Access control for MSK
  "kafka.security.protocol": "SASL_SSL",
  "kafka.sasl.mechanism": "AWS_MSK_IAM",
  "kafka.sasl.jaas.config": "software.amazon.msk.auth.iam.IAMLoginModule required;",
  "kafka.sasl.client.callback.handler.class": "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
}

streaming_data = glueContext.create_data_frame.from_options(
  connection_type="kafka",
  connection_options=kafka_options,
  transformation_ctx="kafka_df"
)

def processBatch(data_frame, batch_id):
  if data_frame.count() > 0:
    stream_data_dynf = DynamicFrame.fromDF(
      data_frame, glueContext, "from_data_frame"
    )

    tables_df = spark.sql(f"SHOW TABLES IN {CATALOG}.{DATABASE}")
    table_list = tables_df.select('tableName').rdd.flatMap(lambda x: x).collect()
    if f"{TABLE_NAME}" not in table_list:
      error_msg = f"Table {TABLE_NAME} doesn't exist in {CATALOG}.{DATABASE}."
      print(f"[ERROR] {error_msg}")
      raise RuntimeError(error_msg)
    else:
      # print(f"Table {TABLE_NAME} exists in {CATALOG}.{DATABASE}.")

      _df = spark.sql(f"SELECT * FROM {CATALOG}.{DATABASE}.{TABLE_NAME} LIMIT 0")

      #XXX: Apply De-duplication logic on input data to pick up the latest record based on timestamp and operation
      stream_data_df = stream_data_dynf.toDF()

      cdc_df = stream_data_df.select(col('after.*'),
        col('op').alias('_op'),
        col('ts_ms').alias('_op_timestamp'))
      cdc_df = cdc_df.withColumn('_op_timestamp', to_timestamp(col('_op_timestamp')/1e3)) \
                     .withColumn('trans_datetime', to_timestamp(col('trans_datetime')/1e3))

      window = Window.partitionBy(PRIMARY_KEY).orderBy(desc("_op_timestamp"))

      deduped_cdc_df = cdc_df.withColumn("row", row_number().over(window)) \
        .filter(col("row") == 1).drop("row") \
        .select(_df.schema.names)

      upsert_data_df = deduped_cdc_df.filter(col('_op') != 'd')
      if upsert_data_df.count() > 0:
        upsert_data_df.createOrReplaceTempView(f"{TABLE_NAME}_upsert")
        # print(f"Table '{TABLE_NAME}' is upserting...")

        try:
          spark.sql(f"""MERGE INTO {CATALOG}.{DATABASE}.{TABLE_NAME} t
            USING {TABLE_NAME}_upsert s ON s.{PRIMARY_KEY} = t.{PRIMARY_KEY}
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
            """)
        except Exception as ex:
          traceback.print_exc()
          raise ex

      deleted_data_df = deduped_cdc_df.filter(col('_op') == 'd')
      if deleted_data_df.count() > 0:
        deleted_data_df.createOrReplaceTempView(f"{TABLE_NAME}_delete")
        # print(f"Table '{TABLE_NAME}' is deleting...")

        try:
          spark.sql(f"""MERGE INTO {CATALOG}.{DATABASE}.{TABLE_NAME} t
            USING {TABLE_NAME}_delete s ON s.{PRIMARY_KEY} = t.{PRIMARY_KEY}
            WHEN MATCHED THEN DELETE
            """)
        except Exception as ex:
          traceback.print_exc()
          raise ex


checkpointPath = os.path.join(args["TempDir"], args["JOB_NAME"], "checkpoint/")

glueContext.forEachBatch(
  frame=streaming_data,
  batch_function=processBatch,
  options={
    "windowSize": WINDOW_SIZE,
    "checkpointLocation": checkpointPath,
  }
)

job.commit()
