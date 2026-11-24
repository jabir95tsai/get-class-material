// Minimal DOM for the production page.evaluate callback. No browser or network.
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const { script, credentials, pageUrl, action, method, detached, override } =
    JSON.parse(readFileSync(0, "utf8"));

const location = new URL(pageUrl);
const form = { action: new URL(action, location).href, method };
const user = { value: "", form };
const pass = { value: "", form };
let clicks = 0;
const submit = {
    form,
    hasAttribute: name => name === override,
    click() { clicks++; },
};
const controls = {
    "#ContentPlaceHolder1_UsernameTextBox": user,
    "#ContentPlaceHolder1_PasswordTextBox": pass,
    "#ContentPlaceHolder1_SubmitButton": submit,
};
if (detached) controls[detached].form = null;
const document = { querySelector: selector => controls[selector] ?? null };
const result = runInNewContext(script, { location, document, URL })(credentials);
process.stdout.write(JSON.stringify({ result, user: user.value, pass: pass.value, clicks }));
