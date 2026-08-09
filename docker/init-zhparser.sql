-- 中文全文检索（zhparser + SCWS）初始化
-- 首次创建数据卷时执行；应用 init_db 也会在 schema 重建后恢复同一合同。

CREATE EXTENSION IF NOT EXISTS zhparser;

DROP TEXT SEARCH CONFIGURATION IF EXISTS chinese;
CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);

-- 映射词性到 simple 字典（保留有意义的实词，丢弃标点等）
--   n=名词  v=动词  a=形容词  i=习语
--   e=叹词  l=临时词  j=简略词  t=时间词
ALTER TEXT SEARCH CONFIGURATION chinese
    ADD MAPPING FOR n,v,a,i,e,l,j,t WITH simple;
