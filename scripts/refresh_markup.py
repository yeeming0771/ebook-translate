"""Refresh unaccepted units after a protected-markup encoder change."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ebook_translate import core
from zipfile import ZipFile


def refresh(job):
    with core.locked(job):
        data = core.load(job)
        roots = {}
        changes = []
        with ZipFile(Path(job) / 'source.epub') as archive:
            for batch in data['batches']:
                for unit in batch['units']:
                    if unit['field'] or 'metadata_index' in unit:
                        continue
                    if unit['document'] not in roots:
                        roots[unit['document']] = core.parse(archive.read(unit['document']))
                    node = roots[unit['document']]
                    for index in unit['path']:
                        node = node[index]
                    source = core.slots(node)[0]
                    if source != unit['source']:
                        if batch['status'] == 'accepted':
                            raise core.JobError('已验收批次的编码将变化，拒绝自动迁移：'+batch['id'])
                        changes.append((batch, unit, source))
        if changes:
            backup = Path(job) / ('markup-backup-' + core.now().replace(':', '-') + '.json')
            core.atomic_json(backup, data)
            for batch, unit, source in changes:
                unit.update(source=source, source_hash=core.digest(source.encode('utf-8')))
                batch.update(status='needs_revision' if batch['attempts'] else 'pending', translation=None)
                batch.pop('approval', None)
            data['markup_version'] = 2
            core.save(job, data)
        return len(changes)


if __name__ == '__main__':
    print('Refreshed units:', refresh(sys.argv[1]))
