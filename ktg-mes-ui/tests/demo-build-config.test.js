const assert = require('assert')
const path = require('path')
const babel = require('@babel/core')
const Service = require('@vue/cli-service/lib/Service')

// Reproduce a clean CLI invocation; a custom mode otherwise defaults Babel to development.
delete process.env.NODE_ENV
delete process.env.BABEL_ENV
const root = path.resolve(__dirname, '..')
const service = new Service(root)
service.init('demo')
assert.strictEqual(process.env.NODE_ENV, 'production')
assert.strictEqual(process.env.BABEL_ENV, 'production')
const config = service.resolveChainableWebpackConfig()
assert.strictEqual(config.output.get('path'), path.join(root, 'dist-demo'))
assert.ok(config.module.rule('js').use('cache-loader').get('options').cacheIdentifier.endsWith(':production'))
const transformed = babel.transformSync('export const load = view => import(`@/views/${view}`)', {
  cwd: root,
  filename: path.join(root, 'src/store/modules/permission.js')
}).code
assert.ok(transformed.includes('import('), 'Business routes must remain webpack lazy imports')
console.log('demo build configuration preserves lazy business routes')
