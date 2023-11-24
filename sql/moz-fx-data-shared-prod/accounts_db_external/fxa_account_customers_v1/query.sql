SELECT
  TO_HEX(uid) AS uid,
  stripeCustomerId,
  SAFE.TIMESTAMP_MILLIS(SAFE_CAST(createdAt AS INT)) AS createdAt,
  SAFE.TIMESTAMP_MILLIS(SAFE_CAST(updatedAt AS INT)) AS updatedAt,
FROM
  EXTERNAL_QUERY(
    "moz-fx-fxa-prod.us.fxa-rds-prod-prod-fxa",
    """SELECT
         uid,
         stripeCustomerId,
         createdAt,
         updatedAt
       FROM
         fxa.accountCustomers
    """
  )
