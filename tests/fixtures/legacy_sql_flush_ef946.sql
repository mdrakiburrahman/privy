DECLARE @rows TABLE(kind nvarchar(32), id nvarchar(400), payload nvarchar(max),
    owner nvarchar(64), revision bigint, at float, target nvarchar(300),
    lease_id nvarchar(80), client_id nvarchar(80));
INSERT @rows SELECT * FROM OPENJSON(?)
WITH(kind nvarchar(32), id nvarchar(400), payload nvarchar(max) AS JSON,
     owner nvarchar(64), revision bigint, at float, target nvarchar(300),
     lease_id nvarchar(80), client_id nvarchar(80));
DECLARE @latest TABLE(kind nvarchar(32), id nvarchar(400), payload nvarchar(max),
    owner nvarchar(64), revision bigint, at float, target nvarchar(300),
    lease_id nvarchar(80), client_id nvarchar(80), PRIMARY KEY(kind,id));
INSERT @latest SELECT kind,id,payload,owner,revision,at,target,lease_id,client_id
FROM (SELECT *,ROW_NUMBER() OVER(PARTITION BY kind,id ORDER BY revision DESC) AS n FROM @rows) ranked
WHERE n=1;
IF EXISTS(SELECT 1 FROM @latest n JOIN dbo.privy_records p WITH(UPDLOCK,HOLDLOCK)
    ON p.kind=n.kind AND p.id=n.id WHERE n.kind='job' AND p.owner<>n.owner)
    THROW 51001, 'Privy job ownership conflict', 1;
UPDATE p SET payload=n.payload,owner=n.owner,revision=n.revision,at=n.at,
    target=n.target,lease_id=n.lease_id,client_id=n.client_id,updated=SYSUTCDATETIME()
FROM dbo.privy_records p WITH(UPDLOCK,HOLDLOCK)
JOIN @latest n ON p.kind=n.kind AND p.id=n.id
WHERE n.kind<>'output' AND (
    (p.owner=n.owner AND p.revision<n.revision) OR
    (n.kind<>'job' AND p.owner<>n.owner AND p.at<n.at));
INSERT dbo.privy_records(kind,id,payload,owner,revision,at,target,lease_id,client_id)
SELECT n.kind,n.id,n.payload,n.owner,n.revision,n.at,n.target,n.lease_id,n.client_id
FROM @latest n WHERE n.kind<>'output' AND NOT EXISTS(
    SELECT 1 FROM dbo.privy_records p WITH(UPDLOCK,HOLDLOCK) WHERE p.kind=n.kind AND p.id=n.id);
INSERT dbo.privy_output(job_id,chunk_index,stream,start_offset,end_offset,payload)
SELECT JSON_VALUE(n.payload,'$.job_id'),CAST(JSON_VALUE(n.payload,'$.index') AS bigint),
    JSON_VALUE(n.payload,'$.stream'),CAST(JSON_VALUE(n.payload,'$.start') AS bigint),
    CAST(JSON_VALUE(n.payload,'$.end') AS bigint),n.payload
FROM @latest n WHERE n.kind='output' AND NOT EXISTS(
    SELECT 1 FROM dbo.privy_output p WITH(UPDLOCK,HOLDLOCK)
    WHERE p.job_id=JSON_VALUE(n.payload,'$.job_id')
      AND p.chunk_index=CAST(JSON_VALUE(n.payload,'$.index') AS bigint));
