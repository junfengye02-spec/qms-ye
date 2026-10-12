import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function readRepositoryFile(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

test('scan approval heading is present in source and Simplified Chinese catalogs', () => {
  const englishCatalog = readRepositoryFile(
    '../../src/locales/en/messages.po'
  );
  const chineseCatalog = readRepositoryFile(
    '../../src/locales/zh_Hans/messages.po'
  );

  assert.match(
    englishCatalog,
    /msgid "Scan Stock Approval"\nmsgstr "Scan Stock Approval"/
  );
  assert.match(
    chineseCatalog,
    /msgid "Scan Stock Approval"\nmsgstr "扫码库存审批"/
  );
});

test('backend scan approval settings have Simplified Chinese translations', () => {
  const backendCatalog = readRepositoryFile(
    '../../../backend/InvenTree/locale/zh_Hans/LC_MESSAGES/django.po'
  );

  const expectedTranslations = [
    ['Scan Stock Approval', '扫码库存审批'],
    ['Scan Approval Draft Timeout', '扫码审批草稿超时'],
    ['Scan Approval Pending Timeout', '扫码审批待审超时'],
    ['Scan Approval Maximum Lines', '扫码审批最大行数'],
    ['minutes', '分钟']
  ];

  for (const [source, translation] of expectedTranslations) {
    assert.match(
      backendCatalog,
      new RegExp(`msgid "${source}"\\nmsgstr "${translation}"`)
    );
  }
});
