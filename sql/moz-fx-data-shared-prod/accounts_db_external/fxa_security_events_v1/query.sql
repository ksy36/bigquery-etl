SELECT
  id,
  TO_HEX(uid) AS uid,
  nameId,
  SAFE_CAST(verified AS BOOL) AS verified,
  ipAddrHmac,
  SAFE.TIMESTAMP_MILLIS(SAFE_CAST(createdAt AS INT)) AS createdAt,
  tokenVerificationId,
FROM
  EXTERNAL_QUERY(
    "moz-fx-fxa-prod.us.fxa-rds-prod-prod-fxa",
    """SELECT
         id,
         uid,
         nameId,
         verified,
         ipAddrHmac,
         createdAt,
         tokenVerificationId
       FROM
         fxa.securityEvents
    """
  )
