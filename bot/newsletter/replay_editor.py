"""Re-run the Bot editor from immutable report snapshots; no database queries."""
import argparse
import hashlib
import json
from pathlib import Path
from .editor import edit_findings
from .render import render


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def replay(source, output, ask):
    payload = json.loads((source / 'newsletter.json').read_text())
    audit = []
    (output/'picks').mkdir(exist_ok=True)
    cards = payload['section03']['cards']
    for card in cards:
        cat, pl = card['category'], card['platform']
        key = cat + '_' + pl
        report_path = source / 'picks' / (key + '_report.json')
        report = json.loads(report_path.read_text())
        if not report.get('ok'):
            from sqlalchemy import event
            from bot.db.connection import get_engine
            engine=get_engine()
            if not getattr(engine,'_newsletter_readonly',False):
                @event.listens_for(engine,'connect')
                def guard(conn,rec):
                    cur=conn.cursor();cur.execute('SET SESSION TRANSACTION READ ONLY');cur.execute('SET SESSION MAX_EXECUTION_TIME=60000');cur.close()
                engine._newsletter_readonly=True
            from .scoped_evidence import run_scoped_report
            print('Retry failed report:',cat,card['brand'],pl,flush=True)
            report=run_scoped_report(card['brand'],payload['period'],pl,cat)
        save(output/'picks'/(key+'_report.json'),report)
        if not report.get('ok') or report.get('meta', {}).get('business_category') != cat:
            raise ValueError(f'{key}: Scoped report unavailable: {report.get('meta')}')
        audit.append(dict(file=str(report_path.relative_to(source)), sha256=hashlib.sha256(report_path.read_bytes()).hexdigest()))
        print('Editor replay:', cat, card['brand'], pl, flush=True)
        card.pop('error_type',None)
        card.update(edit_findings(ask, key, report.get('evidence', []), cat, card['brand'], pl, payload['period']))
        card['status'] = 'SEMANTIC_REVIEW_REQUIRED' if card['findings'] else 'METRICS_ONLY'
        # Checkpoints keep useful output if a later model call fails.
        save(output / 'newsletter.json', payload)
    payload['provenance'] = 'Original SQL metrics and picks retained; Bot editor rerun on hashed report snapshots; no human-written findings'
    complete = len(cards) == 4 and all({f['kind'] for f in c['findings']} == {'product', 'channel'} for c in cards)
    save(output / 'newsletter.json', payload)
    (output / 'newsletter.html').write_text(render(payload))
    save(output / 'validation.json', dict(status='DRAFT_REVIEW' if complete else 'INCOMPLETE_EVIDENCE',
         fresh_queries=0, all_product_channel_findings=complete, reports=audit,
         source_newsletter_sha256=hashlib.sha256((source / 'newsletter.json').read_bytes()).hexdigest(),
         template_sha256=hashlib.sha256(Path(__file__).with_name('approved-template.html').read_bytes()).hexdigest(),
         requires_review=['LLM semantic support', 'product-source versus store scope', 'low-base interpretation']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from bot.config import get_env
    get_env('DASHSCOPE_API_KEY')
    from bot.utils import llm_client, extract_json_object
    def ask(stage, prompt, material):
        save(args.output / (stage + '_request.json'), dict(system=prompt, input=material))
        result = llm_client(max_retries=0).chat.completions.create(
            model='glm-5.2', messages=[dict(role='system', content=prompt), dict(role='user', content=json.dumps(material, ensure_ascii=False))],
            response_format={'type':'json_object'}, max_tokens=2000, timeout=180, extra_body={'enable_thinking':False})
        raw = result.choices[0].message.content or ''
        (args.output / (stage + '_raw.txt')).write_text(raw)
        answer = extract_json_object(raw)
        save(args.output / (stage + '_response.json'), answer)
        return answer
    try:
        replay(args.source, args.output, ask)
    except Exception as exc:
        save(args.output / 'failure.json', dict(stage='editor_replay', error_type=type(exc).__name__))
        raise
    print('Editor replay complete; inspect validation.json and newsletter.html', flush=True)

if __name__ == '__main__': main()
