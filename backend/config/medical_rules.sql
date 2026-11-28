PRAGMA foreign_keys = ON;
BEGIN;

CREATE TABLE exam_catalog (
    exam_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL UNIQUE,
    department TEXT,
    service_minutes INTEGER CHECK (service_minutes > 0)
);

CREATE TABLE physiological_conditions (
    condition_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL UNIQUE,
    assessment TEXT NOT NULL
);

CREATE TABLE exam_condition_rules (
    exam_id TEXT PRIMARY KEY NOT NULL REFERENCES exam_catalog(exam_id),
    preparation TEXT NOT NULL,
    timing_or_prerequisite TEXT NOT NULL,
    confirmation_notes TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed'))
);

CREATE TABLE exam_condition_links (
    exam_id TEXT NOT NULL REFERENCES exam_condition_rules(exam_id),
    condition_id TEXT NOT NULL REFERENCES physiological_conditions(condition_id),
    PRIMARY KEY (exam_id, condition_id)
);

CREATE TABLE precedence_rules (
    rule_id TEXT PRIMARY KEY NOT NULL,
    before_node TEXT NOT NULL,
    after_node TEXT NOT NULL,
    before_exam_id TEXT REFERENCES exam_catalog(exam_id),
    after_exam_id TEXT REFERENCES exam_catalog(exam_id),
    timing TEXT NOT NULL,
    strength TEXT NOT NULL CHECK (strength IN ('H', 'H/C', 'C', 'S')),
    origin TEXT NOT NULL CHECK (origin IN ('direct', 'derived')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed'))
);

CREATE TABLE exam_precedence_rules (
    rule_id TEXT PRIMARY KEY NOT NULL CHECK (rule_id GLOB 'EP-*' AND length(rule_id) > 3),
    before_exam_id TEXT NOT NULL REFERENCES exam_catalog(exam_id),
    after_exam_id TEXT NOT NULL REFERENCES exam_catalog(exam_id),
    recommended_gap_minutes INTEGER CHECK (recommended_gap_minutes >= 0),
    requires_prior_result INTEGER NOT NULL DEFAULT 0 CHECK (requires_prior_result IN (0, 1)),
    CHECK (before_exam_id <> after_exam_id),
    UNIQUE (before_exam_id, after_exam_id)
);

CREATE TABLE state_transition_rules (
    transition_id TEXT PRIMARY KEY NOT NULL,
    condition_id TEXT NOT NULL REFERENCES physiological_conditions(condition_id),
    trigger_events TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed'))
);

INSERT INTO exam_catalog (exam_id, name, department, service_minutes) VALUES
    ('EX01', '血常规', '检验科', 5),
    ('EX02', 'CRP/PCT', '检验科', 5),
    ('EX03', '肝功能', '检验科', 5),
    ('EX04', '肾功能/电解质', '检验科', 5),
    ('EX05', '空腹血糖', '检验科', 5),
    ('EX06', 'HbA1c', '检验科', 5),
    ('EX07', '血脂', '检验科', 5),
    ('EX08', '甲状腺功能', '检验科', 5),
    ('EX09', '激素检测', '检验科', 5),
    ('EX10', '凝血功能', '检验科', 5),
    ('EX11', '血培养', '检验科', 10),
    ('EX12', '尿常规/培养', '检验科', 10),
    ('EX13', '粪便检查', '检验科', 10),
    ('EX14', 'OGTT', '检验科', 120),
    ('EX15', '肝胆超声', '超声科', 20),
    ('EX16', '全腹部超声', '超声科', 25),
    ('EX17', '肾脏超声', '超声科', 15),
    ('EX18', '膀胱超声', '超声科', 15),
    ('EX19', '盆腔经腹超声', '超声科', 20),
    ('EX20', '心脏超声', '超声科', 25),
    ('EX21', '颅脑经囟门超声', '超声科', 20),
    ('EX22', '甲状腺/浅表超声', '超声科', 15),
    ('EX23', '髋关节超声', '超声科', 20),
    ('EX24', '胸部/骨龄X线', '放射科', 10),
    ('EX25', 'CT平扫', '放射科', 15),
    ('EX26', '增强CT', '放射科', 30),
    ('EX27', 'MRI平扫', '放射科', 30),
    ('EX28', '增强MRI', '放射科', 45),
    ('EX29', '镇静MRI', '放射科', 60),
    ('EX30', '心电图', '心电科', 15),
    ('EX31', '动态心电图', '心电科', 30),
    ('EX32', '常规脑电图', '心电科', 30),
    ('EX33', '睡眠脑电图', '心电科', 60),
    ('EX34', '肺功能', '呼吸科', 20),
    ('EX35', '支气管舒张试验', '呼吸科', 40),
    ('EX36', 'FeNO', '呼吸科', 15),
    ('EX37', '睡眠监测PSG', '呼吸科', 480),
    ('EX38', '散瞳眼底检查', '放射科', 40),
    ('EX39', '胃镜', '放射科', 45),
    ('EX40', '肠镜', '放射科', 60);

INSERT INTO physiological_conditions (condition_id, name, assessment) VALUES
    ('S01', '空腹', '根据最后摄入时间及类型判断'),
    ('S02', '膀胱充盈', '根据临床评估或检查准备状态判断'),
    ('S03', '膀胱排空', '判断是否满足排空后检查条件'),
    ('S04', '清醒/配合', '判断是否具备检查配合能力'),
    ('S05', '睡眠', '判断睡眠相关检查条件'),
    ('S06', '镇静/麻醉状态', '根据监护及恢复评估判断'),
    ('S07', '药物影响状态', '判断特定检验或功能检查要求'),
    ('S08', '肠道准备状态', '根据清肠完成标准判断'),
    ('S09', '运动后恢复状态', '判断心电、呼吸等检查准备要求');

INSERT INTO exam_condition_rules (exam_id, preparation, timing_or_prerequisite, confirmation_notes) VALUES
    ('EX01', '通常无需空腹', '无固定禁食窗口', '检验SOP；是否与其他空腹项目合并'),
    ('EX02', '通常无需空腹', '无固定禁食窗口', '检验SOP；不同检测方法的要求'),
    ('EX03', '视检验组合', '由检验科规定', '是否需要空腹及具体时长'),
    ('EX04', '通常无需统一空腹', '视项目及病情', '脱水、输液和采样要求'),
    ('EX05', '空腹', '常见要求至少8小时；儿童按医嘱', '年龄分层、低血糖风险'),
    ('EX06', '无需空腹', '无', '是否与其他采血合并'),
    ('EX07', '依筛查方案', '空腹或非空腹方案', '何种情况需要复查空腹血脂'),
    ('EX08', '通常无需空腹', '关注用药及采血时间', '药物服用时间、特殊检验要求'),
    ('EX09', '时间、体位、应激状态', '特定采样时刻或间隔', '具体激素对应的采样方案'),
    ('EX10', '用药状态', '按医嘱', '抗凝药物、采样条件'),
    ('EX11', '尽量抗菌治疗前采样', '临床允许时优先采样', '急诊例外、采血组数'),
    ('EX12', '清洁或规范留尿', '采样后送检时限', '尿袋、清洁中段尿、导尿适用情况'),
    ('EX13', '合格标本', '采样后送检时限', '标本保存与特殊饮食要求'),
    ('EX14', '规定空腹及定时采血', '基线采血、饮糖、规定时间点采血', '年龄、剂量、采样时间点'),
    ('EX15', '空腹', '按年龄及项目确定', '婴幼儿喂养间隔、能否饮水'),
    ('EX16', '空腹/膀胱充盈，视器官', '按扫描方案', '是否需同时满足两种状态'),
    ('EX17', '通常无需特殊准备', '无或按方案', '是否包含膀胱检查'),
    ('EX18', '膀胱适度充盈', '检查时达到要求', '饮水量、充盈判定、年龄差异'),
    ('EX19', '膀胱适度充盈', '检查时达到要求', '不同年龄及病情的准备方式'),
    ('EX20', '安静、配合', '检查期间', '婴幼儿自然睡眠或镇静流程'),
    ('EX21', '体位、配合', '适用于可用声窗', '适用年龄及特殊准备'),
    ('EX22', '通常无需特殊准备', '无', '是否需要特殊体位'),
    ('EX23', '体位、配合', '检查期间', '年龄、固定及配合要求'),
    ('EX24', '体位、静止', '曝光期间', '陪同、防护、体位要求'),
    ('EX25', '静止', '扫描期间', '是否需镇静及相应准备'),
    ('EX26', '静止、造影剂安全条件', '注射前完成风险评估', '静脉通路、过敏史、肾功能评估适应证'),
    ('EX27', '静止、MRI安全条件', '扫描前安全筛查', '金属植入物、镇静需求'),
    ('EX28', 'MRI安全条件、造影剂评估', '增强前完成相关评估', '造影剂种类、肾功能评估适应证'),
    ('EX29', '禁食禁饮、镇静评估', '按麻醉方案及年龄', '禁食时长、镇静流程、恢复标准'),
    ('EX30', '安静', '采集期间', '运动后是否需要休息'),
    ('EX31', '连续佩戴', '通常24小时或医嘱指定', '佩戴时长、设备归还时间'),
    ('EX32', '头皮清洁、配合', '检查前准备', '睡眠、药物及头发清洁要求'),
    ('EX33', '睡眠状态', '按年龄调整睡眠', '睡眠剥夺程度、是否自然睡眠'),
    ('EX34', '配合、呼吸状态', '检查前避免影响结果的因素', '年龄下限、支气管药物停用规则'),
    ('EX35', '基础测量、给药、复测', '按药物和方案规定间隔', '药物剂量、等待时长、复测次数'),
    ('EX36', '呼吸状态、饮食及活动控制', '按检测规程', '与肺功能检查的先后安排'),
    ('EX37', '自然睡眠', '夜间或规定监测周期', '入睡时间、设备安装、陪护'),
    ('EX38', '散瞳状态', '滴药后等待起效', '药物、次数、起效时间'),
    ('EX39', '空腹、可能需麻醉', '按内镜与麻醉方案', '禁食禁饮、恢复观察'),
    ('EX40', '肠道准备、可能需麻醉', '检查前完成清肠', '清肠方案、完成判定及麻醉要求');

INSERT INTO exam_condition_links (exam_id, condition_id) VALUES
    ('EX01', 'S01'),
    ('EX02', 'S01'),
    ('EX03', 'S01'),
    ('EX04', 'S01'),
    ('EX05', 'S01'),
    ('EX06', 'S01'),
    ('EX07', 'S01'),
    ('EX08', 'S01'),
    ('EX08', 'S07'),
    ('EX10', 'S07'),
    ('EX11', 'S07'),
    ('EX14', 'S01'),
    ('EX15', 'S01'),
    ('EX16', 'S01'),
    ('EX16', 'S02'),
    ('EX18', 'S02'),
    ('EX19', 'S02'),
    ('EX20', 'S04'),
    ('EX21', 'S04'),
    ('EX23', 'S04'),
    ('EX24', 'S04'),
    ('EX25', 'S04'),
    ('EX26', 'S04'),
    ('EX27', 'S04'),
    ('EX29', 'S01'),
    ('EX29', 'S06'),
    ('EX30', 'S04'),
    ('EX30', 'S09'),
    ('EX32', 'S04'),
    ('EX33', 'S05'),
    ('EX34', 'S04'),
    ('EX35', 'S07'),
    ('EX37', 'S05'),
    ('EX38', 'S07'),
    ('EX39', 'S01'),
    ('EX39', 'S06'),
    ('EX40', 'S08'),
    ('EX40', 'S06');

INSERT INTO precedence_rules (rule_id, before_node, after_node, after_exam_id, timing, strength, origin) VALUES
    ('D01', 'OGTT基线采血', '口服葡萄糖', NULL, '基线采血完成后', 'H', 'direct'),
    ('D02', '口服葡萄糖', 'OGTT定时采血', NULL, '按具体试验时间点', 'H', 'direct'),
    ('D03', '基础肺功能', '支气管舒张剂给药', NULL, '完成基线后', 'H', 'direct'),
    ('D04', '支气管舒张剂给药', '复测肺功能', NULL, '按药物及检测方案', 'H', 'direct'),
    ('D05', '膀胱充盈成像', '排尿', NULL, '需先完成充盈状态采集', 'H/C', 'direct'),
    ('D06', '排尿', '残余尿测量', NULL, '按院方规定尽快测量', 'H', 'direct'),
    ('D07', '散瞳滴药', '散瞳眼底检查', 'EX38', '药物起效后', 'H', 'direct'),
    ('D08', 'MRI安全筛查', 'MRI扫描', NULL, '扫描前完成', 'H', 'direct'),
    ('D09', '造影剂安全评估', '增强CT/MRI', NULL, '造影剂注射前', 'H', 'direct'),
    ('D10', '麻醉评估', '镇静/麻醉检查', NULL, '检查前完成', 'H', 'direct'),
    ('D11', '镇静/麻醉检查', '恢复评估', NULL, '检查后', 'H', 'direct'),
    ('D12', '恢复达到标准', '离院或转运', NULL, '达标后', 'H', 'direct'),
    ('D13', '血培养采样', '首次抗菌药物给药', NULL, '临床允许时', 'C', 'direct'),
    ('D14', '尿培养采样', '首次抗菌药物给药', NULL, '临床允许时', 'C', 'direct'),
    ('D15', '肠道准备完成', '肠镜', 'EX40', '满足清肠及禁食要求后', 'H', 'direct'),
    ('D16', '监测设备佩戴', '动态监测结束', NULL, '达到规定监测时长', 'H', 'direct'),
    ('D17', '空腹项目完成', '解除空腹限制', NULL, '所有相关禁食要求解除后', 'C', 'derived'),
    ('D18', '需清醒配合的检查', '镇静检查', NULL, '无统一强制窗口', 'S', 'direct');

INSERT INTO state_transition_rules (transition_id, condition_id, trigger_events) VALUES
    ('T01', 'S01', '进食、饮用不允许的液体'),
    ('T02', 'S02', '饮水、尿液生成、排尿'),
    ('T03', 'S03', '排尿、导尿'),
    ('T04', 'S04', '睡眠、镇静、疲劳等'),
    ('T05', 'S05', '入睡、唤醒、睡眠剥夺'),
    ('T06', 'S06', '镇静给药、恢复'),
    ('T07', 'S07', '给药、停药、药效变化'),
    ('T08', 'S08', '清肠方案、排便'),
    ('T09', 'S09', '运动、休息');

COMMIT;
