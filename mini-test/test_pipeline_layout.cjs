const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const renderer = fs.readFileSync(path.join(__dirname, '../src/memory/reporting/project_pipeline.py'), 'utf8');
const script = renderer.match(/<script>\r?\n([\s\S]*?)<\/script>/)[1];

function makePipeline(workflowCount, depth) {
  const pipeline = {summary:'Sample', warnings:[], workflows:[], components:[], edges:[], outcomes:[]};
  for (let lane = 0; lane < workflowCount; lane += 1) {
    const workflow = `workflow-${lane}`;
    const component = step => `component-${lane}-${step}`;
    pipeline.workflows.push({id:workflow, name:workflow, trigger_component_id:component(0)});
    for (let step = 0; step < depth; step += 1) {
      const id = component(step);
      pipeline.components.push({id, name:id, layer:'core', workflow_ids:[workflow]});
      if (step) pipeline.edges.push({id:`link-${id}`, from_component_id:component(step - 1),
        to_component_id:id, relation_types:['calls'], workflow_ids:[workflow]});
    }
    pipeline.outcomes.push({id:`result-${lane}`, component_id:component(depth - 1), workflow_id:workflow,
      label:'Result', kind:'return', observed_terminal:true});
  }
  return pipeline;
}

// Run the generated viewer script through its normal initialization and filter handlers.
function openViewer(viewerScript) {
  class Element {
    value = '';
    style = {};
    children = [];
    handlers = new Map();
    addEventListener(event, handler) { this.handlers.set(event, handler); }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.append(child); }
    querySelectorAll(selector) {
      const inputs = this.children.flatMap(child => child.children).filter(child => child.type === 'checkbox');
      return selector === 'input:checked' ? inputs.filter(input => input.checked) : inputs;
    }
  }
  const elements = new Map();
  const frames = [];
  const graph = {};
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement: () => new Element(),
    createTextNode: text => text,
  };
  const vis = {
    DataSet: class { constructor(items = []) { this.items = items; } },
    Network: class {
      on() {}
      fit() {}
      focus() {}
      setData({nodes, edges}) { Object.assign(graph, {nodes:nodes.items, edges:edges.items}); }
    },
  };
  vm.runInNewContext(viewerScript, {document, vis, requestAnimationFrame: callback => frames.push(callback)});
  frames.splice(0).forEach(callback => callback());
  return {graph, elements};
}

function renderFixture(workflows, depth) {
  const viewerScript = script.replace('__PIPELINE_DATA__', () => JSON.stringify(makePipeline(workflows, depth)));
  return openViewer(viewerScript);
}

function aspect(nodes) {
  const xs = nodes.map(node => node.x);
  const ys = nodes.map(node => node.y);
  return (Math.max(...xs) - Math.min(...xs) + 250) / (Math.max(...ys) - Math.min(...ys) + 88);
}

test('dense stages fill a compact area with distinct node positions', () => {
  const {graph} = renderFixture(45, 6);
  assert.equal(graph.nodes.length, 360);
  assert.equal(graph.edges.length, 315);
  assert.equal(new Set(graph.nodes.map(({x, y}) => `${x}:${y}`)).size, graph.nodes.length);
  assert.ok(aspect(graph.nodes) < 2, `Expected a compact footprint, got ${aspect(graph.nodes)}`);
});

test('a deep pipeline changes direction at an adjacent column and continues on the next band', () => {
  const {graph} = renderFixture(1, 28);
  const nodeById = new Map(graph.nodes.map(node => [node.id, node]));
  const segments = Array.from(graph.edges, ({from, to}) => [nodeById.get(from), nodeById.get(to)]);
  assert.ok(segments.some(([from, to]) => from.x === to.x && from.y < to.y));
  assert.ok(segments.some(([from, to]) => from.x > to.x && from.y === to.y));
  assert.ok(aspect(graph.nodes) < 2.5);
});

test('a filtered short workflow keeps its left-to-right order and reset restores the original positions', () => {
  const {graph, elements} = renderFixture(45, 3);
  const initial = JSON.stringify(graph.nodes);
  const filter = elements.get('workflow-filter');
  filter.value = 'workflow-0';
  filter.handlers.get('change')();
  const nodeById = new Map(graph.nodes.map(node => [node.id, node]));
  assert.equal(nodeById.size, 5);
  assert.equal(graph.edges.length, 4);
  graph.edges.forEach(({from, to}) => assert.ok(nodeById.get(from).x < nodeById.get(to).x));
  elements.get('reset').handlers.get('click')();
  assert.equal(JSON.stringify(graph.nodes), initial);
});
