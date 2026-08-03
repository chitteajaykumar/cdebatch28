from pyspark.sql.functions import col, lit
from pyspark.sql.types import StringType

df_csv = spark.read.format("csv") \
    .option("header", "true") \
    .option("inferSchema", "true") \
    .load("s3://ajay-databricks-demo-bucket/movies_01.csv")

filter_df = df_csv.filter((col("release_year") == 2024) & (col("imdb_rating") > 8))

filter_df1 = filter_df.filter(col("industry").isin("Tollywood", "Mollywood"))

add_df = filter_df1.withColumn("Review", lit(None).cast(StringType()))

df_fillna = add_df.fillna({"Review": "avg"})

df_fillna.write.format("delta").mode("overwrite").save("s3://sampledatabricks023/output/")