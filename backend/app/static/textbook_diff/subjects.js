/**
 * STEP 04 学科配置（首页 / workbench / compare 共用）。
 * id 用于 URL ?subject=；label 对应 volumes.subject。
 * DISTRIBUTION_MODE=test_diff 时仅保留 Test。
 */
(function (global) {
  const STORAGE_KEY = 'textbook_diff_subject';
  const distributionMode = String(global.__DISTRIBUTION_MODE__ || '').toLowerCase();
  const isTestDiffDist = distributionMode === 'test_diff';

  /** 小科年级上下册（对齐旧库小学科学册次网格） */
  function xiaokeGrades(minGrade, maxGrade) {
    const out = [];
    for (let g = minGrade; g <= maxGrade; g += 1) {
      out.push({ grade: g, term: '上', label: `${g}年级上册` });
      out.push({ grade: g, term: '下', label: `${g}年级下册` });
    }
    return out;
  }

  /** 数学等初中学科年级上下册（与 xiaokeGrades 同结构，仅命名区分） */
  function mathGrades(minGrade, maxGrade) {
    const out = [];
    for (let g = minGrade; g <= maxGrade; g += 1) {
      out.push({ grade: g, term: '上', label: `${g}年级上册` });
      out.push({ grade: g, term: '下', label: `${g}年级下册` });
    }
    return out;
  }

  const ALL_SUBJECTS = [
    {
      id: 'shuxue',
      label: '数学',
      editions: [
        {
          id: 'jijiao',
          label: '冀教版',
          code_prefix: 'SXJJ',
          school_system: '63',
          grades: mathGrades(7, 9),
        },
      ],
    },
  ];

  const SUBJECTS = isTestDiffDist
    ? ALL_SUBJECTS.filter((s) => s.id === 'test')
    : ALL_SUBJECTS;
  const DEFAULT_SUBJECT_ID = 'shuxue';

  /** 沙箱学科：Test / 小科（本地 JSON、上传弹窗、一键识别） */
  function isSandboxSubjectId(id) {
    const key = String(id || '').trim().toLowerCase();
    return key === 'test' || key === 'xiaoke';
  }

  function getSubjectById(id) {
    const key = String(id || '').trim().toLowerCase();
    return SUBJECTS.find((s) => s.id === key) || null;
  }

  function getSubjectByLabel(label) {
    const name = String(label || '').trim();
    return SUBJECTS.find((s) => s.label === name) || null;
  }

  function resolveSubjectId(raw) {
    const key = String(raw || '').trim().toLowerCase();
    // 原「科学」入口已并入小科；旧书签 / localStorage 自动转向
    if (key === 'kexue' || String(raw || '').trim() === '科学') {
      return 'xiaoke';
    }
    if (getSubjectById(key)) return key;
    const byLabel = getSubjectByLabel(raw);
    if (byLabel) return byLabel.id;
    return '';
  }

  function readSubjectFromQuery() {
    try {
      return resolveSubjectId(new URLSearchParams(window.location.search).get('subject'));
    } catch (e) {
      return '';
    }
  }

  function loadStoredSubjectId() {
    try {
      return resolveSubjectId(localStorage.getItem(STORAGE_KEY));
    } catch (e) {
      return '';
    }
  }

  function saveSubjectId(id) {
    const resolved = resolveSubjectId(id);
    if (!resolved) return;
    try {
      localStorage.setItem(STORAGE_KEY, resolved);
    } catch (e) { /* ignore */ }
  }

  /** URL query → localStorage → 默认学科 */
  function resolveActiveSubjectId(fallback) {
    return (
      readSubjectFromQuery()
      || loadStoredSubjectId()
      || resolveSubjectId(fallback)
      || DEFAULT_SUBJECT_ID
    );
  }

  function subjectQuery(id) {
    const resolved = resolveSubjectId(id) || DEFAULT_SUBJECT_ID;
    return `subject=${encodeURIComponent(resolved)}`;
  }

  global.DIFF_SUBJECTS = SUBJECTS;
  global.DIFF_SUBJECT_STORAGE_KEY = STORAGE_KEY;
  global.diffSubjectById = getSubjectById;
  global.diffSubjectByLabel = getSubjectByLabel;
  global.diffResolveSubjectId = resolveSubjectId;
  global.diffResolveActiveSubjectId = resolveActiveSubjectId;
  global.diffSaveSubjectId = saveSubjectId;
  global.diffSubjectQuery = subjectQuery;
  global.diffIsSandboxSubjectId = isSandboxSubjectId;
})(typeof window !== 'undefined' ? window : globalThis);
