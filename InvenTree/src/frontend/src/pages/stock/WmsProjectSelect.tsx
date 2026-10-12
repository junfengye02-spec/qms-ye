import { t } from '@lingui/core/macro';
import {
  Alert,
  Button,
  Group,
  Modal,
  Select,
  Stack,
  Table,
  Text,
  TextInput
} from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../App';
import { wmsError, wmsUrl } from './WmsInventoryPanel';

export type WmsProject = {
  pk: number;
  code: string;
  description: string;
  active: boolean;
};

function useProjects() {
  return useQuery<{ results: WmsProject[] }>({
    queryKey: ['wms-projects'],
    queryFn: async () => (await api.get(wmsUrl('projects'))).data
  });
}

export function WmsProjectSelect({
  value,
  onChange,
  label = t`项目号`,
  required = true
}: {
  value: string;
  onChange: (code: string, projectId?: number) => void;
  label?: string;
  required?: boolean;
}) {
  const projects = useProjects();
  const queryClient = useQueryClient();
  const [opened, setOpened] = useState(false);
  const [code, setCode] = useState('');
  const [description, setDescription] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function createProject() {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const response = await api.post<WmsProject>(wmsUrl('projects'), {
        code: code.trim(),
        description: description.trim()
      });
      const project = response.data;
      queryClient.setQueryData<{ results: WmsProject[] }>(
        ['wms-projects'],
        (current) => ({
          results: [
            ...(current?.results ?? []).filter(
              (item) => item.pk !== project.pk
            ),
            project
          ].sort((a, b) => a.code.localeCompare(b.code))
        })
      );
      onChange(project.code, project.pk);
      setOpened(false);
    } catch (err) {
      const codeErrors = (err as any)?.response?.data?.code;
      setError(
        Array.isArray(codeErrors) &&
          codeErrors.every((item) => typeof item === 'string')
          ? codeErrors.join('；')
          : wmsError(err)
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Group align='end' wrap='nowrap'>
        <Select
          style={{ flex: 1 }}
          required={required}
          searchable
          clearable
          label={label}
          placeholder={t`选择已有项目，没有对应项目时先新建`}
          value={value || null}
          data={(projects.data?.results ?? []).map((item) => ({
            value: item.code,
            label: item.code
          }))}
          onChange={(next) =>
            onChange(
              next ?? '',
              projects.data?.results.find((item) => item.code === next)?.pk
            )
          }
          error={projects.isError ? t`项目加载失败，请重新打开页面` : undefined}
        />
        <Button
          variant='light'
          onClick={() => {
            setCode('');
            setDescription('');
            setError('');
            setOpened(true);
          }}
        >{t`新建项目`}</Button>
      </Group>
      <Modal
        opened={opened}
        onClose={() => setOpened(false)}
        title={t`新建项目`}
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void createProject();
          }}
        >
          <Stack>
            {error && <Alert color='red'>{error}</Alert>}
            <Text size='sm'>{t`项目号唯一；同一项目可被多张入库单和库存记录重复引用。`}</Text>
            <TextInput
              required
              label={t`项目号`}
              maxLength={100}
              value={code}
              onChange={(event) => setCode(event.currentTarget.value)}
            />
            <TextInput
              label={t`项目说明`}
              maxLength={200}
              value={description}
              onChange={(event) => setDescription(event.currentTarget.value)}
            />
            <Button
              type='submit'
              loading={busy}
              disabled={!code.trim()}
            >{t`保存项目`}</Button>
          </Stack>
        </form>
      </Modal>
    </>
  );
}

export function WmsProjectManager() {
  const projects = useProjects();
  const [selected, setSelected] = useState('');
  return (
    <Stack>
      <Text size='sm'>{t`项目号统一管理，每个编号仅保存一份。填写入库单时选择已有项目；同一项目可以使用多个合同。`}</Text>
      <WmsProjectSelect
        value={selected}
        onChange={setSelected}
        required={false}
      />
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>{t`项目号`}</Table.Th>
            <Table.Th>{t`项目说明`}</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {(projects.data?.results ?? [])
            .filter((item) => !selected || item.code === selected)
            .map((item) => (
              <Table.Tr key={item.pk}>
                <Table.Td>{item.code}</Table.Td>
                <Table.Td>{item.description || '—'}</Table.Td>
              </Table.Tr>
            ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
