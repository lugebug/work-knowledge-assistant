"""Read-only coverage check for project responsibility questions."""
from judgment_cards import JudgmentCards
from review_requirements import known, requirements


def status(store):
    with store.connect() as conn:
        conn.execute('BEGIN')
        cards = JudgmentCards(store).queue_in_connection(conn)
        rows = []
        for project in store._records(conn, 'projects').values():
            if project.get('archived'):
                continue
            missing = []
            if not (known(project, 'owner_id') or known(project, 'owner')):
                missing.append('owner')
            if not known(project, 'my_role'):
                missing.append('my_role')
            if not missing:
                continue
            coverage = {}
            protected = {}
            for field in missing:
                keys = {'owner_id', 'owner'} if field == 'owner' else {field}
                coverage[field] = [card['id'] for card in cards if card['state'] in ('pending', 'deferred')
                    and any(t['kind'] == 'projects' and t['id'] == project['id'] and keys.intersection(t['values']) for t in card['targets'])]
                protected[field] = [card['id'] for card in cards if card['state'] in ('resolved', 'dismissed')
                    and any(t['kind'] == 'projects' and t['id'] == project['id'] and keys.intersection(t['any_of']) for t in requirements(card))]
            rows.append({'id': project['id'], 'name': project['name'], 'missing': missing,
                         'questions': coverage, 'protected_history':protected,
                         'uncovered': [field for field, ids in coverage.items() if not ids and not protected[field]]})
        return {'projects_with_gaps': len(rows), 'uncovered_projects': sum(bool(r['uncovered']) for r in rows), 'items': rows}
