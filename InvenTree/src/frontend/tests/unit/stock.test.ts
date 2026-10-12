import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canAccessScanStockNavigation,
  stockAvailableQuantity,
  stockFrozenQuantity
} from '../../src/functions/stock.ts';

test('scan-stock navigation is available to stock operators', () => {
  assert.equal(
    canAccessScanStockNavigation({
      approvalEnabled: false,
      canViewStock: true,
      canChangeStock: true
    }),
    true
  );
});

test('scan-stock navigation is available to approval-only users', () => {
  assert.equal(
    canAccessScanStockNavigation({
      approvalEnabled: true,
      canViewStock: true,
      canChangeStock: false,
      scanApprovalPermissions: ['approve', 'view']
    }),
    true
  );
});

test('scan-stock navigation is available to audit-only users', () => {
  assert.equal(
    canAccessScanStockNavigation({
      approvalEnabled: true,
      canViewStock: true,
      canChangeStock: false,
      scanApprovalPermissions: ['audit', 'view']
    }),
    true
  );
});

test('scan-stock navigation stays hidden from read-only stock users', () => {
  assert.equal(
    canAccessScanStockNavigation({
      approvalEnabled: true,
      canViewStock: true,
      canChangeStock: false,
      scanApprovalPermissions: ['view']
    }),
    false
  );
});

test('approval-only access requires scan approval to be enabled', () => {
  assert.equal(
    canAccessScanStockNavigation({
      approvalEnabled: false,
      canViewStock: true,
      canChangeStock: false,
      scanApprovalPermissions: ['approve', 'view']
    }),
    false
  );
});

test('server available quantity is authoritative', () => {
  assert.equal(
    stockAvailableQuantity({
      quantity: 20,
      allocated: 3,
      scan_frozen: 4,
      available_quantity: 7
    }),
    7
  );
});

test('fallback availability subtracts allocations and frozen scans', () => {
  assert.equal(
    stockAvailableQuantity({ quantity: 20, allocated: 3, scan_frozen: 4 }),
    13
  );
});

test('availability and frozen quantities are clamped at zero', () => {
  assert.equal(
    stockAvailableQuantity({
      quantity: '2',
      allocated: '1',
      scan_frozen: '5'
    }),
    0
  );
  assert.equal(stockFrozenQuantity({ scan_frozen: '-3' }), 0);
});

test('invalid server availability falls back to component quantities', () => {
  assert.equal(
    stockAvailableQuantity({
      quantity: 10,
      allocated: 2,
      scan_frozen: 1,
      available_quantity: 'not-a-number'
    }),
    7
  );
});
