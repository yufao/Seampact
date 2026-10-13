// Local QA against the first-party loopback server; no external browser profile.
const {chromium}=require(process.env.SEAMPACT_PLAYWRIGHT || 'playwright');
(async()=>{
 const browser=await chromium.launch({...(process.env.SEAMPACT_CHROME ? {executablePath:process.env.SEAMPACT_CHROME} : {}),headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1080}});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto(process.env.SEAMPACT_URL || 'http://127.0.0.1:8874');
 await page.waitForSelector('#source-cards .card');
 if(await page.locator('#source-cards .card').count()!==4)throw Error('four source cards missing');
 const frozen = page.waitForResponse(response => response.url().endsWith('/api/freeze') && response.request().method() === 'POST');
 await page.getByRole('button',{name:'仅准备来源',exact:true}).click();
 if(!(await frozen).ok())throw Error('source preparation failed');
 await page.locator('#models').scrollIntoViewIfNeeded();
 if(await page.locator('#model-cards input[type=password]').count()!==2)throw Error('provider forms missing');
 await page.evaluate(()=>window.scrollTo(0,0));
 await page.screenshot({path:process.argv[2],fullPage:true});
 await page.setViewportSize({width:390,height:844});
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('mobile overflow');
 if(errors.length)throw Error(errors.join('; '));
 console.log(JSON.stringify({ui:'PASS',sources:4,provider_forms:2,page_errors:errors,mobile_overflow:false}));
 await browser.close();
})().catch(error=>{console.error(error.message);process.exit(1)});
