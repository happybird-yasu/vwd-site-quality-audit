function renderRanking(code,name,data){
  const panel=document.getElementById('detailPanel');
  let info;
  if(currentAttr==='total'){
    info=data[currentYear]&&data[currentYear][currentDir]&&data[currentYear][currentDir][code];
  }else{
    info=data[currentYear]&&data[currentYear][currentDir]&&data[currentYear][currentDir][currentSub]&&data[currentYear][currentDir][currentSub][code];
    if(currentAttr==='nationality'&&currentSub==='foreign'&&currentYear==='2019'){
      info=null;
    }
  }
  const dirLabel=currentDir==='inflow'?t('inflow'):t('outflow');
  const attrLabel=currentAttr==='total'?'':` ／ ${ATTR_CONFIG[currentAttr].subs?ATTR_CONFIG[currentAttr].subs.find(s=>s.key===currentSub)?.label:''}`;

  // タブを毎回新規生成
  panel.innerHTML='';
  panel.appendChild(renderAttrTabs());
  const subTabs=buildSubTabs();
  if(subTabs)panel.appendChild(subTabs);

  if(!info||!info.top10||!info.top10.length){
    const noData=document.createElement('div');
    noData.innerHTML=`<div class="eyebrow">${currentYear}${t('year_suffix')} ／ ${dirLabel}${attrLabel}</div><h2>${name}</h2><div class="placeholder" style="padding-top:20px">${currentAttr==='nationality'&&currentSub==='foreign'&&currentYear==='2019'?t('foreign_2019_unavailable'):t('condition_out_of_scope')}</div>`;
    panel.appendChild(noData);
    return;
  }
  const max=info.top10[0].value;
  const rankLabel=currentDir==='inflow'?t('inflow_ranking'):t('outflow_ranking');
  panel.innerHTML='';
  panel.appendChild(renderAttrTabs());
  const subTabs2=buildSubTabs();
  if(subTabs2)panel.appendChild(subTabs2);
  const content=document.createElement('div');
  content.innerHTML=
    `<div class="eyebrow">${currentYear}${t('year_suffix')} ／ ${dirLabel}${attrLabel}</div>`+
    `<h2>${name}</h2>`+
    `<div class="total-num">${info.total.toLocaleString()}<span>${t('total_people',{direction:dirLabel})}</span></div>`+
    `<div class="rank-label">${rankLabel}</div>`+
    info.top10.map((r,i)=>
      `<div class="rank-row">`+
      `<span class="no">${i+1}</span>`+
      `<span class="name"><a href="${SLUG_MAP[r.code.substring(0,2)]+'.html?city='+r.code}" style="color:inherit;text-decoration:underline;text-underline-offset:2px;">${r.name}</a></span>`+
      `<div class="bar-bg"><div class="bar-fill" style="width:${(r.value/max*100).toFixed(0)}%"></div></div>`+
      `<span class="val">${r.value.toLocaleString()}${t('people_suffix')}</span>`+
      `</div>`
    ).join('');
  panel.appendChild(content);
}

