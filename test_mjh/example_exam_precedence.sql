-- 候选集测试数据，顺序和间隔均为假设值。
INSERT INTO exam_precedence_rules (
    rule_id, before_exam_id, after_exam_id, recommended_gap_minutes, requires_prior_result
) VALUES
    ('EP-DEMO-01', 'EX01', 'EX06', 10, 0),
    ('EP-DEMO-02', 'EX06', 'EX05', 5, 0),
    ('EP-DEMO-03', 'EX15', 'EX18', NULL, 0);
