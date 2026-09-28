from pi_index.parsers.xjtu import parse_xjtu_teacher_profile


def test_xjtu_teacher_profile_extracts_research_and_contact_fields():
    html = """
    <html><body>
      <div class="jiaoshi">
        <h1>丁宁</h1>
        <p>研究领域（方向）</p>
        <p>大模型、人机交互、自然语言处理、语音处理。</p>
        <p>个人及工作简历</p>
        <p>2023年8月-至今，西安交通大学，教授。</p>
        <p>联系方式</p>
        <p>电子邮箱：ding.ning@xjtu.edu.cn</p>
        <p>个人主页：</p>
        <p>https://gr.xjtu.edu.cn/zh/web/ding.ning</p>
      </div>
    </body></html>
    """
    config = {"parsing": {"default_title": "博士生导师", "default_department": "人工智能学院"}}

    people = parse_xjtu_teacher_profile(
        html,
        "https://www.xjtu.edu.cn/jsnr.jsp?urltype=tree.TreeTempUrl&wbtreeid=1632&wbwbxjtuteacherid=3155",
        config,
    )

    assert len(people) == 1
    person = people[0]
    assert person.name == "丁宁"
    assert person.title == "博士生导师"
    assert person.department == "人工智能学院"
    assert person.emails == ["ding.ning@xjtu.edu.cn"]
    assert person.lab_url == "https://gr.xjtu.edu.cn/zh/web/ding.ning"
    assert person.research_areas == ["大模型", "人机交互", "自然语言处理", "语音处理"]
